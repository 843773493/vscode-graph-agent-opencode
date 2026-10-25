"""真实Node/PTY/Chromium孤儿删除独立推进，整批到期保留原lease并由管理入口核实。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.workspace_resources import owner as owner_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_orphan_reclaim_keeps_one_budget_and_reconciles_a_lost_delete_receipt(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    _browser_client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        now = datetime.now(UTC)
        monkeypatch.setattr(owner, "_clock", lambda: now)
        resources = []
        for number in range(2):
            arguments = {
                "session_id": session_id, "retention_scope": "thread",
                "holder_id": f"request:orphan-create:{number}", "operation_id": f"orphan-create:{number}",
            }
            if kind == "terminal":
                resources.append(await owner.create_terminal(**arguments))
            else:
                resources.append(await owner.create_browser(**arguments, url="about:blank"))
        rid, peer = (str(item["resource_id"]) for item in resources)
        for resource_id in (rid, peer):
            await owner.command(
                resource_id, action="detach", expected_revision=1,
                thread=owner._store.get(resource_id).source_thread,
                holder_id=f"request:orphan-detach:{resource_id}", operation_id="orphan-detach",
            )
        revision_entered, revision_release = asyncio.Event(), asyncio.Event()
        delete_entered, delete_release, peer_deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_sync, original_delete = owner._transport.sync_owner_revision, owner._transport.delete_resource
        deadlines: list[asyncio.Timeout] = []
        deletes: list[str] = []

        def capture(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            context = asyncio.timeout(seconds)
            if asyncio.current_task().get_name() == "orphan-sweep-under-test":
                deadlines.append(context)
            return context

        async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
            await original_sync(resource_id, revision, for_stop=for_stop)
            if resource_id == rid and for_stop:
                revision_entered.set()
                await revision_release.wait()

        async def deleting(resource_id: str) -> dict[str, object]:
            deletes.append(resource_id)
            receipt = await original_delete(resource_id)
            if resource_id == rid:
                delete_entered.set()
                await delete_release.wait()
            else:
                peer_deleted.set()
            return receipt

        monkeypatch.setattr(owner_module, "timeout", capture, raising=False)
        monkeypatch.setattr(owner._transport, "sync_owner_revision", syncing)
        monkeypatch.setattr(owner._transport, "delete_resource", deleting)
        now += timedelta(seconds=1800)
        sweeping = asyncio.create_task(owner.sweep_orphans(), name="orphan-sweep-under-test")
        try:
            await asyncio.wait_for(revision_entered.wait(), 5)
            await asyncio.wait_for(peer_deleted.wait(), 5)
            assert not sweeping.done()
            current = await owner.get_resource(peer)
            assert current["status"] == "deleted" and current["reconciliation_status"] == "verified"
            assert len(deadlines) == 1
            deadline = deadlines[0].when()
            revision_release.set()
            await asyncio.wait_for(delete_entered.wait(), 5)
            assert len(deadlines) == 1 and deadlines[0].when() == deadline
            pending, = owner._leases.leases_for_resource(rid)
            assert pending.status == "active"
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError, match="孤儿回收等待超过 30 秒"):
                await asyncio.wait_for(sweeping, 5)
            saved, = owner._leases.leases_for_resource(rid)
            record = owner._store.get(rid)
            assert saved.lease_id == pending.lease_id and saved.status == "reconcile_required"
            assert record.status == "deleting" and record.reconciliation_status == "unknown"
            assert record.owner_revision == 3
            external = await owner._transport.get_resource(rid)
            assert external["status"] == "deleted" and external["reconciliation_status"] == "verified"
            assert external["transport_owner_revision"] == 3
            stored = json.loads((root / f".boxteam/{kind}-manager/{kind}s.json").read_text())
            persisted = {item[f"{kind}_id"]: item for item in stored[f"{kind}s"]}
            assert set(persisted) == {rid, peer}
            assert all(item["status"] == "deleted" and item["reconciliation_status"] == "verified"
                       and item["transport_owner_revision"] == 3 for item in persisted.values())
            assert await owner.sweep_orphans() == []
            assert deletes.count(rid) == deletes.count(peer) == 1
            delete_release.set()
            reconciled = await owner.command(
                rid, action="delete", expected_revision=3,
                holder_id="request:orphan-confirm", operation_id="orphan-confirm",
            )
            assert reconciled["status"] == "deleted" and reconciled["owner_revision"] == 3
            assert owner._leases.get_lease(pending.lease_id).status == "settled"
            assert not owner._leases.leases_for_resource(rid)
            assert deletes.count(rid) == deletes.count(peer) == 1
            run.write_result("passed", {
                "kind": kind, "resource_id": rid, "peer_id": peer, "owner_revision": 3,
                "lease_id": pending.lease_id, "delete_calls": deletes, "budget_seconds": 30,
                "scope": "正式Session/创建/detach/孤儿回收与管理核实，真实Node/PTY或Chromium/HTTP；外置推进原截止并暂停真实回执，不代表不合作进程终止、Gateway/Web/Windows或完整shutdown",
            })
        finally:
            revision_release.set()
            delete_release.set()
            await asyncio.gather(sweeping, return_exceptions=True)
