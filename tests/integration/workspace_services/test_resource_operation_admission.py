"""正式创建及真实辅助服务验证工具准入失败不派发、不丢失原占用。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import operations as operations_module
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
async def test_real_admission_keeps_original_resource(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        ledger_path = owner._leases._state_path
        assert ledger_path is not None
        tid = container.session_catalog.resolver.main_thread_id(sid)
        async with resource_api(container, kind) as client:
            resources = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"admit-{number}",
                })
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"]["resource_id"])
        rid, peer = resources
        external = await owner._transport.get_resource(rid)
        deadlines: list[asyncio.Timeout] = []
        calls: list[str] = []
        recovered, continue_admission = asyncio.Event(), asyncio.Event()
        recover = owner.recover
        method = "read_terminal" if kind == "terminal" else "perform"
        dispatch = getattr(owner._transport, method)

        def capture(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            context = asyncio.timeout(seconds)
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "admit-under-test":
                deadlines.append(context)
            return context

        async def recovering(resource_ids=None) -> None:
            await recover(resource_ids)
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "admit-under-test":
                recovered.set()
                await continue_admission.wait()

        async def observed(resource_id: str, *args, **kwargs):
            calls.append(resource_id)
            return await dispatch(resource_id, *args, **kwargs)

        async with owner.operation(session_id=sid, thread_id=tid,
                                   holder_id="request:admit", operation_id="admit") as operation:
            async def read(resource_id: str):
                if isinstance(operation, TerminalOperations):
                    return await operation.read_terminal(resource_id)
                return await operation.perform(resource_id, "read", {})

            await read(rid)
            active, = owner._leases.leases_for_resource(rid)
            assert active.status == "active"
            lock = owner._resource_lock(rid)
            with monkeypatch.context() as patch:
                patch.setattr(operations_module, "timeout", capture, raising=False)
                patch.setattr(owner._transport, method, observed)
                patch.setattr(owner, "recover", recovering)
                pending = asyncio.create_task(read(rid), name="admit-under-test")
                try:
                    # 真实恢复完成后，由测试持原锁，明确阻塞工具准入而非恢复的子任务。
                    await asyncio.wait_for(recovered.wait(), 5)
                    await lock.acquire()
                    continue_admission.set()
                    await asyncio.sleep(0)
                    assert not pending.done()
                    await asyncio.wait_for(read(peer), 5)
                    before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                    # typeshed未公开Task的等待者，仅测试读取CPython原future以核精确取消。
                    admission_waiter = getattr(pending, "_fut_waiter")  # noqa: B009
                    assert admission_waiter is not None and admission_waiter in (lock._waiters or ())
                    if deadlines:
                        deadline = deadlines[0].when()
                        assert deadline is not None
                        deadlines[0].reschedule(deadline - 31)
                    with pytest.raises(TimeoutError, match="operation 准入等待超过 30 秒") as caught:
                        await asyncio.wait_for(asyncio.shield(pending), 0.5)
                    assert len(deadlines) == 1 and deadlines[0].expired()
                    assert rid in str(caught.value) and str(root) not in str(caught.value)
                    assert lock.locked()
                    assert admission_waiter.cancelled() and admission_waiter not in (lock._waiters or ())
                    assert calls == [peer]
                    assert owner._store.path.read_bytes() == before
                    assert ledger_path.read_bytes() == ledger
                    lock.release()
                    await asyncio.sleep(0)
                    assert calls == [peer]
                    cold = ExternalResourceLeaseLedger(state_path=ledger_path)
                    assert cold.get_lease(active.lease_id) == active
                    current = await owner._transport.get_resource(rid)
                    identity = "os_pid" if kind == "terminal" else "active_page_id"
                    assert current[identity] == external[identity]
                    assert current["transport_owner_revision"] == external["transport_owner_revision"] == 1
                    assert current["status"] == "running"
                    await read(rid)
                    assert calls == [peer, rid]
                    assert owner._leases.get_lease(active.lease_id) == active
                    record = owner._store.get(rid)
                    assert record is not None and record.reconciliation_status == "verified"
                    (run.artifacts_dir / f"admit-{kind}.json").write_text(json.dumps({
                        "error": str(caught.value), "external": current, "lease_id": active.lease_id,
                        "calls": calls, "scope": "正式Session/ASGI创建与原operation；真实PTY或Chromium/HTTP；推进本次截止，不代表真实等待30秒、Gateway/Web或Backend重启",
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                finally:
                    continue_admission.set()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if lock.locked():
                        lock.release()
        assert not owner._leases.leases_for_resource(rid)
