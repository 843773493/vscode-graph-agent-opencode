"""真实Node/PTY/Chromium孤儿删除独立推进，整批到期保留原lease并由管理入口核实。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.workspace_resources import owner as owner_module
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import terminal_plugin_gateway_for_workspace
from tests.harness.python.terminal_state import read_terminal_state
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
async def resource_workspace(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    integration_config_path: str, unused_tcp_port: int,
    prepared_developer_tools_package: Path, kind: str,
) -> AsyncIterator[tuple[MountedWorkspace, TestRunContext]]:
    with TestRunContext.from_test_file(request.path) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / kind,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        install_test_workspace_config(
            workspace_root=root, config_path=Path(integration_config_path),
            schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
        )
        home = run.boxteam_home_for_node(request.node.nodeid)
        monkeypatch.setenv("BOXTEAM_HOME", str(home))
        async with terminal_plugin_gateway_for_workspace(
            root, run.artifacts_dir, unused_tcp_port, home,
            prepared_package_root=prepared_developer_tools_package, plugin_ids=frozenset({kind}),
        ):
            yield MountedWorkspace(load_or_create_workspace_id(root), root), run


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_orphan_reclaim_keeps_one_budget_and_reconciles_a_lost_delete_receipt(
    resource_workspace: tuple[MountedWorkspace, TestRunContext],
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    owner_mount, run = resource_workspace
    root = owner_mount.workspace_root
    # 本场景直接装配正式 owner，避免另一个周期 worker 抢先回收被测候选。
    # 完整 Workspace 启停由 lifecycle 场景验证，这里仍使用真实 Session、SQL 和插件 HTTP。
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        session = await container.session_service.create(SessionCreateRequest(title="孤儿回收核实"))
        session_id = session.session_id
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
        retained_arguments = {
            "session_id": session_id, "retention_scope": "workspace",
            "holder_id": "request:workspace-retained", "operation_id": "workspace-retained",
        }
        if kind == "terminal":
            retained = await owner.create_terminal(**retained_arguments)
        else:
            retained = await owner.create_browser(**retained_arguments, url="about:blank")
        retained_id = str(retained["resource_id"])
        detached = await owner.command(
            retained_id, action="detach", expected_revision=1,
            thread=owner._store.get(retained_id).source_thread,
            holder_id="request:workspace-detach", operation_id="workspace-detach",
        )
        assert detached["thread_associations"] == []
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

        async def syncing(resource_id: str, revision: int, *, for_stop: bool, **revision_options: object) -> None:
            await original_sync(resource_id, revision, for_stop=for_stop, **revision_options)
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
            assert pending.status == "dispatching"
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
            stored = (
                read_terminal_state(root / ".boxteam/terminal-manager/terminals.sqlite")
                if kind == "terminal"
                else json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
            )
            persisted = {item[f"{kind}_id"]: item for item in stored[f"{kind}s"]}
            assert set(persisted) == {rid, peer, retained_id}
            assert all(item["status"] == "deleted" and item["reconciliation_status"] == "verified"
                       and item["transport_owner_revision"] == 3 for resource_id, item in persisted.items()
                       if resource_id in {rid, peer})
            assert persisted[retained_id]["status"] == "running"
            assert persisted[retained_id]["transport_owner_revision"] == 2
            assert await owner.sweep_orphans() == []
            assert deletes.count(rid) == deletes.count(peer) == 1
            kept = await owner.get_resource(retained_id)
            assert kept["status"] == "running" and kept["reconciliation_status"] == "verified"
            assert kept["retention_scope"] == "workspace" and kept["thread_associations"] == []
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
                "retained_workspace_resource_id": retained_id,
                "scope": "正式Session/创建/detach/孤儿回收与管理核实，真实Node/PTY或Chromium/HTTP；外置推进原截止并暂停真实回执，不代表不合作进程终止、Gateway/Web/Windows或完整shutdown",
            })
        finally:
            revision_release.set()
            delete_release.set()
            await asyncio.gather(sweeping, return_exceptions=True)
            await owner.command(
                retained_id, action="delete", expected_revision=2,
                holder_id="request:workspace-cleanup", operation_id="workspace-cleanup",
            )
