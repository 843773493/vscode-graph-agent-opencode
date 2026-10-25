"""正式资源详情入口在锁及真实清单回执等待到期后保留原资源与占用。"""

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
async def test_real_detail_deadline_keeps_the_live_resource_and_original_lease_for_retry(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, stage: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        tid = container.session_catalog.resolver.main_thread_id(sid)
        async with resource_api(container, kind) as client:
            resources = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"detail-{number}",
                })
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"]["resource_id"])
            rid, peer = resources
            external = await owner._transport.get_resource(rid)
            entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
            recover, listing = owner.recover, owner._transport.list_resources
            lock = owner._resource_lock(rid)
            deadlines: list[asyncio.Timeout] = []
            received: list[str] = []

            def selected() -> bool:
                return asyncio.current_task().get_name() == "resource-detail-under-test"

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

            async def receiving():
                result = await listing()
                if selected():
                    received.append(rid)
                    if stage == "receipt":
                        entered.set()
                        try:
                            await release.wait()
                        finally:
                            drained.set()
                return result

            async with owner.operation(
                session_id=sid, thread_id=tid, holder_id="request:detail-holder", operation_id="detail-holder",
            ) as operation:
                if kind == "terminal":
                    await operation.read_terminal(rid)
                else:
                    await operation.perform(rid, "read", {})
                active, = owner._leases.leases_for_resource(rid)
                assert active.status == "active"
                with monkeypatch.context() as patch:
                    patch.setattr(owner_module, "timeout", capture)
                    patch.setattr(owner, "recover", recovering)
                    patch.setattr(owner._transport, "list_resources", receiving)
                    pending = asyncio.create_task(client.get(f"/api/v1/resources/{rid}", headers={
                        "X-Request-ID": "resource-detail-under-test",
                    }), name="resource-detail-under-test")
                    try:
                        await asyncio.wait_for(entered.wait(), 10)
                        independent = await client.get(f"/api/v1/resources/{peer}")
                        assert independent.status_code == 200, independent.text
                        assert independent.json()["data"]["status"] == "running"
                        before = owner._store.path.read_bytes()
                        ledger = owner._leases._state_path.read_bytes()
                        if deadlines:
                            deadlines[0].reschedule(deadlines[0].when() - 31)
                        failed = await asyncio.wait_for(asyncio.shield(pending), 1)
                        assert failed.status_code == 503, failed.text
                        assert failed.headers["X-Request-ID"] == "resource-detail-under-test"
                        assert "TimeoutError" in failed.text and str(root) not in failed.text
                        assert len(deadlines) == 1 and deadlines[0].expired()
                        assert owner._store.path.read_bytes() == before
                        assert owner._leases._state_path.read_bytes() == ledger
                        assert received == ([rid] if stage == "receipt" else [])
                        if stage == "lock":
                            assert lock.locked()
                            lock.release()
                        else:
                            assert drained.is_set()
                        release.set()
                        await asyncio.sleep(0)
                        assert owner._store.path.read_bytes() == before
                        assert owner._leases._state_path.read_bytes() == ledger
                        cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                        assert cold.get_lease(active.lease_id) == active
                    finally:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                        release.set()
                        if stage == "lock" and lock.locked():
                            lock.release()
                current = await owner._transport.get_resource(rid)
                identity = "os_pid" if kind == "terminal" else "active_page_id"
                assert current[identity] == external[identity]
                assert current["transport_owner_revision"] == external["transport_owner_revision"] == 1
                assert current["status"] == "running"
                retried = await client.get(f"/api/v1/resources/{rid}")
                assert retried.status_code == 200, retried.text
                assert retried.json()["request_id"] == retried.headers["X-Request-ID"]
                assert retried.json()["data"]["owner_revision"] == 1
                assert retried.json()["data"]["reconciliation_status"] == "verified"
                assert owner._leases.get_lease(active.lease_id) == active
                (run.artifacts_dir / f"detail-{kind}-{stage}.json").write_text(json.dumps({
                    "failure": failed.json(), "retry": retried.json(), "external": current,
                    "lease_id": active.lease_id,
                    "scope": "正式Session与ASGI资源创建/详情、真实Node/PTY或Chromium/HTTP；只推进本次截止，不代表真实等待30秒、Gateway/Web或Windows原生进程",
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            assert not owner._leases.leases_for_resource(rid)
