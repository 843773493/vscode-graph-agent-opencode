"""整批释放到期保留其它资源的原失败及精确占用，不重放真实删除。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import owner as owner_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.test_resource_release import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_release_retains_completed_failure_when_peer_receipt_expires(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            ids: list[str] = []
            for number in range(3):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"release-errors-{number}",
                })
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
                ids.append(response.json()["data"]["resource_id"])
            original_delete = owner._transport.delete_resource
            failed, entered, release, peer_done = (asyncio.Event() for _ in range(4))
            original_error = ConnectionError("真实删除完成后回执读取失败")
            calls: list[str] = []
            deadlines: list[asyncio.Timeout] = []

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                current = asyncio.current_task()
                if current is not None and current.get_name() == "release-failure-batch":
                    deadlines.append(context)
                return context

            async def deleting(resource_id: str) -> dict[str, object]:
                calls.append(resource_id)
                receipt = await original_delete(resource_id)
                if resource_id == ids[0]:
                    failed.set()
                    raise original_error
                if resource_id == ids[1]:
                    entered.set()
                    await release.wait()
                else:
                    peer_done.set()
                return receipt

            monkeypatch.setattr(owner_module, "timeout", capture)
            monkeypatch.setattr(owner._transport, "delete_resource", deleting)
            task = asyncio.create_task(owner.cleanup_session(sid), name="release-failure-batch")
            try:
                for event in (failed, entered, peer_done):
                    await asyncio.wait_for(event.wait(), 15)
                pending = {rid: owner._leases.leases_for_resource(rid)[0].lease_id for rid in ids[:2]}
                assert len(deadlines) == 1
                deadlines[0].reschedule(asyncio.get_running_loop().time())
                with pytest.raises(ExceptionGroup) as caught:
                    await asyncio.wait_for(task, 5)
                failure, deadline = caught.value.exceptions
                assert failure is original_error
                assert isinstance(deadline, TimeoutError) and "关联释放等待超过 30 秒" in str(deadline)
                assert isinstance(deadline.__cause__, TimeoutError)
                cold_ledger = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                for rid in ids[:2]:
                    record = owner._store.get(rid)
                    assert record is not None
                    assert record.owner_revision == 2 and record.reconciliation_status == "unknown"
                    lease, = cold_ledger.leases_for_resource(rid)
                    assert lease.lease_id == pending[rid] and lease.status == "reconcile_required"
                healthy = owner._store.get(ids[2])
                assert healthy is not None and healthy.status == "deleted"
                assert not cold_ledger.leases_for_resource(ids[2])
                assert not any(owner._resource_lock(rid).locked() for rid in ids)
                persisted = json.loads((root / f".boxteam/{kind}-manager/{kind}s.json").read_text())
                assert {item[f"{kind}_id"] for item in persisted[f"{kind}s"]} == set(ids)
                for item in persisted[f"{kind}s"]:
                    assert item["status"] == "deleted" and item["reconciliation_status"] == "verified"
                    assert item["transport_owner_revision"] == 2
                release.set()
                await owner.cleanup_session(sid)
                assert len(calls) == 3 and set(calls) == set(ids)
                for rid in ids:
                    record = owner._store.get(rid)
                    assert record is not None and record.status == "deleted"
                assert not any(owner._leases.leases_for_resource(rid) for rid in ids)
                run.write_result("passed", {
                    "kind": kind, "resource_ids": ids, "original_error": type(original_error).__name__,
                    "deadline_error": type(deadline).__name__, "lease_ids": pending, "delete_calls": calls,
                    "scope": "正式Session/ASGI创建、原Session资源drain及真实PTY/Chromium/HTTP；暂停真实删除回执并推进原截止，不代表Gateway/Web、Windows、Backend重启或不合作终止验收",
                })
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_operation_close_cancellation_retains_failure_and_original_leases(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, context):
        assert context.thread_binding is not None
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            ids: list[str] = []
            for number in range(3):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"close-cancel-{number}",
                })
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
                ids.append(response.json()["data"]["resource_id"])
            failed, healthy, blocked = ids
            async with owner.operation(session_id=sid, thread_id=context.thread_binding.thread_id,
                                       holder_id="request:real-close-cancel", operation_id="real-close-cancel") as operation:
                for rid in ids:
                    if isinstance(operation, TerminalOperations):
                        await operation.read_terminal(rid)
                    else:
                        await operation.perform(rid, "navigate", {"url": "about:blank"})
                leases = {rid: owner._leases.leases_for_resource(rid)[0] for rid in ids}
                ledger_ids = set(owner._leases._leases)
                records = {rid: owner._store.get(rid) for rid in ids}
                external = {rid: await owner._transport.get_resource(rid) for rid in ids}
                original = OSError("原占用释放写盘失败")
                release = owner._leases.release

                def releasing(lease_id: str):
                    if lease_id == leases[failed].lease_id:
                        raise original
                    return release(lease_id)

                with monkeypatch.context() as patch:
                    patch.setattr(owner._leases, "release", releasing)
                    lock = owner._resource_lock(blocked)
                    async with lock:
                        closing = asyncio.create_task(operation.close())
                        try:
                            async with asyncio.timeout(5):
                                while healthy in operation._lease_refs:
                                    await asyncio.sleep(0)
                            assert not closing.done()
                            closing.cancel("原调用取消")
                            with pytest.raises(asyncio.CancelledError, match="^原调用取消$") as caught:
                                await closing
                            assert closing.cancelled()
                            cause = caught.value.__cause__
                            assert isinstance(cause, BaseExceptionGroup) and cause.exceptions == (original,)
                            expected_refs = {rid: leases[rid].lease_id for rid in (failed, blocked)}
                            assert operation._lease_refs == expected_refs
                            cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                            for rid, lease in leases.items():
                                current = cold.get_lease(lease.lease_id)
                                assert current is not None
                                assert current.status == ("released" if rid == healthy else "active")
                            assert not lock._waiters
                        finally:
                            closing.cancel()
                            await asyncio.gather(closing, return_exceptions=True)
                await asyncio.sleep(0)
                assert operation._lease_refs == expected_refs
                for rid in ids:
                    assert owner._store.get(rid) == records[rid]
                    current = await owner._transport.get_resource(rid)
                    identity = "os_pid" if kind == "terminal" else "active_page_id"
                    assert current["status"] == external[rid]["status"] == "running"
                    assert current[identity] == external[rid][identity]
                    assert current["transport_owner_revision"] == external[rid]["transport_owner_revision"]
                await operation.close()
                await operation.close()
                assert operation._lease_refs == {}
                cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                assert set(cold._leases) == ledger_ids
                for lease in leases.values():
                    current = cold.get_lease(lease.lease_id)
                    assert current is not None and current.status == "released"
                run.write_result("passed", {
                    "kind": kind, "resource_ids": ids, "lease_ids": {rid: lease.lease_id for rid, lease in leases.items()},
                    "entry": "resource-operation-close", "original_error": type(original).__name__,
                    "cancellation": str(caught.value),
                    "scope": "正式Session/ASGI资源创建、原operation及真实PTY/Chromium/HTTP；只注入原lease释放失败和锁等待，不代表Gateway/Web、Windows、Backend重启或不合作终止验收",
                })
