"""正式传输授权入口的锁和真实签发回执共用一次等待预算。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.workspace_resources import owner as owner_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("stage", ["lock", "receipt"])
async def test_real_grant_deadline_protects_unknown_issuance_and_allows_independent_resource(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, stage: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            resources = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "workspace", "title": f"grant-{number}",
                })
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"]["resource_id"])
            rid, peer = resources
            external = await owner._transport.get_resource(rid)
            recover, issue = owner.recover, owner._transport.issue_transport_grant
            lock = owner._resource_lock(rid)
            entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
            deadlines: list[asyncio.Timeout] = []
            issued: list[str] = []

            def selected() -> bool:
                return asyncio.current_task().get_name() == "resource-grant-under-test"

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                if selected():
                    deadlines.append(context)
                return context

            async def recovering(resource_ids=None) -> None:
                await recover(resource_ids)
                if selected() and stage == "lock":
                    await lock.acquire()
                    entered.set()

            async def issuing(resource_id: str, **kwargs):
                result = await issue(resource_id, **kwargs)
                if selected():
                    assert result["grant_token"] and result["owner_revision"] == 1
                    issued.append(resource_id)
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        drained.set()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(owner_module, "timeout", capture)
                patch.setattr(owner, "recover", recovering)
                patch.setattr(owner._transport, "issue_transport_grant", issuing)
                pending = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/transport-grant", json={
                    "expected_revision": 1,
                }, headers={"X-Request-ID": "resource-grant-under-test"}), name="resource-grant-under-test")
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    independent = await client.post(f"/api/v1/resources/{peer}/transport-grant", json={"expected_revision": 1})
                    assert independent.status_code == 200, independent.text
                    assert independent.json()["data"]["grant_token"]
                    before, ledger = owner._store.path.read_bytes(), owner._leases._state_path.read_bytes()
                    active = owner._leases.leases_for_resource(rid)
                    assert len(active) == (1 if stage == "receipt" else 0)
                    if deadlines:
                        deadlines[0].reschedule(deadlines[0].when() - 31)
                    failed = await asyncio.wait_for(asyncio.shield(pending), 1)
                    assert failed.status_code == 503, failed.text
                    assert failed.headers["X-Request-ID"] == "resource-grant-under-test"
                    assert "TimeoutError" in failed.text and str(root) not in failed.text
                    assert len(deadlines) == 1 and deadlines[0].expired()
                    assert issued == ([rid] if stage == "receipt" else [])
                    if stage == "lock":
                        assert lock.locked()
                        assert owner._store.path.read_bytes() == before
                        assert owner._leases._state_path.read_bytes() == ledger
                        lock.release()
                    else:
                        assert drained.is_set()
                        assert owner._store.get(rid).reconciliation_status == "unknown"
                        cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                        retained = cold.get_lease(active[0].lease_id)
                        assert retained.status == "reconcile_required"
                        assert (retained.resource_id, retained.operation_id) == (rid, "resource-grant-under-test")
                    release.set()
                    await asyncio.sleep(0)
                    assert issued == ([rid] if stage == "receipt" else [])
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    release.set()
                    if stage == "lock" and lock.locked():
                        lock.release()
            current = await owner._transport.get_resource(rid)
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            assert current[identity] == external[identity]
            assert current["status"] == "running" and current["transport_owner_revision"] == 1
            queried = await client.get(f"/api/v1/resources/{rid}")
            assert queried.status_code == 200, queried.text
            if stage == "receipt":
                assert queried.json()["data"]["reconciliation_status"] == "unknown"
                assert owner._leases.get_lease(active[0].lease_id).status == "reconcile_required"
                stopped = await client.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": 1})
                assert stopped.status_code == 200, stopped.text
                assert stopped.json()["data"]["owner_revision"] == 2
                assert stopped.json()["data"]["reconciliation_status"] == "verified"
                assert owner._leases.get_lease(active[0].lease_id).status == "settled"
                rejected = await client.post(f"/api/v1/resources/{rid}/transport-grant", json={"expected_revision": 2})
                assert rejected.status_code == 409, rejected.text
            else:
                retried = await client.post(f"/api/v1/resources/{rid}/transport-grant", json={"expected_revision": 1})
                assert retried.status_code == 200, retried.text
                assert retried.json()["data"]["resource"]["owner_revision"] == 1
                assert retried.json()["data"]["grant_token"]
            assert not owner._leases.leases_for_resource(rid)
            # 只保存授权身份和结算事实，不将临时grant token写入诊断产物。
            (run.artifacts_dir / f"grant-{kind}-{stage}.json").write_text(json.dumps({
                "failure": failed.json(), "resource_id": rid, "external_identity": current[identity],
                "issued_count": len(issued), "original_lease": active[0].lease_id if active else None,
                "scope": "正式Session/ASGI创建与传输授权，真实Node/PTY或Chromium/HTTP；仅推进本次截止，不代替真实等待30秒、Gateway/Web或独立进程重启",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
