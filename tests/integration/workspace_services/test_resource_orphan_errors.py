"""真实孤儿删除中断保留此前错误、未知lease及已完成终态。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
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
@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_real_orphan_interruption_preserves_failure_and_reconciles_original_delete_once(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, monkeypatch: pytest.MonkeyPatch, kind: str, ending: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        # 周期worker沿原时刻运行，仅本次回收及其子任务取得到期时间域。
        observed_at = datetime.now(UTC)
        clock = ContextVar("orphan-error-clock", default=observed_at)
        monkeypatch.setattr(owner, "_clock", clock.get)
        async with resource_api(container, kind) as api:
            ids: list[str] = []
            for index in range(3):
                response = await api.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"orphan-errors-{index}",
                })
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
                data = response.json()["data"]
                rid = data["resource_id"]
                ids.append(rid)
                detached = await api.post(f"/api/v1/resources/{rid}/detach", json={
                    "expected_revision": data["owner_revision"], "thread": data["source_thread"],
                })
                assert detached.status_code == 200, detached.text
                assert detached.json()["data"]["thread_associations"] == []
            failed, blocked, healthy = ids
            original_delete = owner._transport.delete_resource
            did_fail, arrived, peer_done = (asyncio.Event() for _ in range(3))
            original_error = ConnectionError("真实孤儿删除已落盘，原回执读取失败")
            calls: list[str] = []
            deadlines: list[asyncio.Timeout] = []

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                current = asyncio.current_task()
                if current is not None and current.get_name() == "real-orphan-error-batch":
                    deadlines.append(context)
                return context

            async def deleting(resource_id: str) -> dict[str, object]:
                calls.append(resource_id)
                receipt = await original_delete(resource_id)
                if resource_id == failed:
                    did_fail.set()
                    raise original_error
                if resource_id == blocked:
                    arrived.set()
                    await asyncio.Future()
                peer_done.set()
                return receipt

            token = clock.set(clock.get() + timedelta(seconds=1800))
            with monkeypatch.context() as patch:
                patch.setattr(owner_module, "timeout", capture)
                patch.setattr(owner._transport, "delete_resource", deleting)
                task = asyncio.create_task(owner.sweep_orphans(), name="real-orphan-error-batch")
                try:
                    for event in (did_fail, arrived, peer_done):
                        await asyncio.wait_for(event.wait(), 15)
                    leases = {rid: owner._leases.leases_for_resource(rid)[0].lease_id for rid in (failed, blocked)}
                    assert len(deadlines) == 1 and not task.done()
                    if ending == "deadline":
                        deadlines[0].reschedule(asyncio.get_running_loop().time())
                        with pytest.raises(ExceptionGroup) as caught:
                            await asyncio.wait_for(task, 5)
                        first, expiry = caught.value.exceptions
                        assert first is original_error
                        assert isinstance(expiry, TimeoutError) and "孤儿回收等待超过 30 秒" in str(expiry)
                        assert isinstance(expiry.__cause__, TimeoutError)
                    else:
                        task.cancel("真实孤儿回收调用者取消")
                        with pytest.raises(asyncio.CancelledError) as cancelled:
                            await task
                        assert cancelled.value.args == ("真实孤儿回收调用者取消",)
                        cause = cancelled.value.__cause__
                        assert isinstance(cause, ExceptionGroup) and cause.exceptions == (original_error,)
                    cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                    for rid, lease_id in leases.items():
                        lease, = cold.leases_for_resource(rid)
                        assert lease.lease_id == lease_id and lease.status == "reconcile_required"
                        record = owner._store.get(rid)
                        assert record is not None and record.status == "deleting"
                        assert record.owner_revision == 3 and record.reconciliation_status == "unknown"
                    assert not any(owner._resource_lock(rid).locked() for rid in ids)
                    for rid in ids:
                        runtime = await owner._transport.get_resource(rid)
                        assert runtime["status"] == "deleted" and runtime["reconciliation_status"] == "verified"
                        assert runtime["transport_owner_revision"] == 3
                    peer = await api.get(f"/api/v1/resources/{healthy}")
                    assert peer.status_code == 200, peer.text
                    assert peer.json()["data"]["status"] == "deleted"
                    assert not cold.leases_for_resource(healthy)
                    assert await owner.sweep_orphans() == []
                    assert len(calls) == 3 and set(calls) == set(ids)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    clock.reset(token)
            for rid, lease_id in leases.items():
                confirmed = await api.post(f"/api/v1/resources/{rid}/delete", json={"expected_revision": 3})
                assert confirmed.status_code == 200, confirmed.text
                assert confirmed.json()["data"]["status"] == "deleted"
                assert confirmed.json()["data"]["owner_revision"] == 3
                assert confirmed.json()["request_id"] == confirmed.headers["X-Request-ID"]
                settled = ExternalResourceLeaseLedger(state_path=owner._leases._state_path).get_lease(lease_id)
                assert settled is not None and settled.status == "settled"
            assert len(calls) == 3 and set(calls) == set(ids)
            assert not any(owner._leases.leases_for_resource(rid) for rid in ids)
            run.write_result("passed", {
                "kind": kind, "ending": ending, "resource_ids": ids, "lease_ids": leases, "delete_calls": calls,
                "scope": "正式Session/ASGI创建/detach/显式delete、原孤儿回收及真实PTY/Chromium/HTTP；删除后暂停回执并推进原截止，不代表真实等候30秒、Gateway/Web、Windows、Backend重启或不合作终止",
            })
