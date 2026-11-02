"""真实消息执行的 Turn 收尾；模型复用 model_stream，辅助 lease 由测试显式准备。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable

import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
    ResourceLease,
    ResourceRecord,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用完整应用生命周期与模型协议资产
)
from tests.support.api_helpers import create_session


@pytest.mark.parametrize("fail_first_write", [False, True], ids=["slow-write", "failed-write"])
async def test_product_turn_cleanup_waits_for_ledger_and_preserves_failed_scope(
    source_case: RuntimeSourceCase,
    monkeypatch: pytest.MonkeyPatch,
    fail_first_write: bool,
) -> None:
    case = source_case
    ledger = case.container.external_resource_leases
    registry = case.container.agent_execution_service.execution_scope_registry
    session_id = await create_session(case.client, "Turn 原账本收尾")
    await case.plan(session_id, [{"content": "Turn 收尾前的真实模型输出"}])
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered, finish = asyncio.Event(), threading.Event()
    streams: list[str] = []
    writer_threads: list[int] = []
    resource_id, lease_id, peer_id = "test-turn-resource", "test-turn-lease", "test-peer-lease"
    commit, release_turn = ledger._commit, ledger.release_turn_leases
    failure = PermissionError(13, "Turn lease 写盘拒绝", str(case.workspace / ".boxteam/resources.json"))

    def observed_release(turn_stream_id: str) -> list[ResourceLease]:
        streams.append(turn_stream_id)
        if ledger.get_lease(lease_id) is None:
            # Session、Thread、Turn、scope 均由真实消息入口生成；这里只准备辅助占用。
            ledger.register(resource_id=resource_id, kind="test", lifetime_scope="workspace")
            for identity, stream in ((lease_id, turn_stream_id), (peer_id, "test-peer-turn")):
                ledger.acquire(resource_id=resource_id, turn_stream_id=stream,
                               lease_id=identity, operation_id=identity)
        return release_turn(turn_stream_id)

    def blocked_commit(
        *, records: Iterable[ResourceRecord] = (), leases: Iterable[ResourceLease] = (),
    ) -> None:
        records, leases = tuple(records), tuple(leases)
        if any(lease.lease_id == lease_id and lease.status == "released" for lease in leases):
            writer_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            # 错误实现立即失败，不让测试自身的屏障阻塞事件循环。
            assert writer_threads[-1] != loop_thread, "Turn 清理仍在事件循环同步写盘"
            # 原 release_turn_leases 此时持有真实账本 RLock，内存尚未发布。
            assert finish.wait(8), "测试未归还原账本写盘屏障"
            if fail_first_write and len(writer_threads) == 1:
                raise failure
        commit(records=records, leases=leases)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "release_turn_leases", observed_release)
        patch.setattr(ledger, "_commit", blocked_commit)
        accepted = await _send(case, session_id, "验证原 Turn 收尾")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        try:
            await asyncio.wait_for(entered.wait(), 20)
            assert len(writer_threads) == 1 and writer_threads[0] != loop_thread
            stream_id, = streams
            scope, inbox = registry.get(stream_id), registry.get_inbox(stream_id)
            assert scope is not None and inbox is not None
            assert scope.lifetime_scope.state == "closing"
            with pytest.raises(RuntimeError, match="Turn 已存在执行 scope"):
                registry.create(stream_id)
            lease = ledger.get_lease(lease_id)
            assert lease is not None and lease.status == "active"
            job = await case.container.job_service.get(job_id)
            assert job.status.value not in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}
            response = await asyncio.wait_for(case.client.get("/api/v1/workspace"), 2)
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
        finally:
            finish.set()

        await _job(case, job_id, "failed" if fail_first_write else "completed")
        if fail_first_write:
            assert registry.get(stream_id) is scope
            assert registry.get_inbox(stream_id) is inbox
            assert scope.lifetime_scope.state == "close_failed"
            lease = ledger.get_lease(lease_id)
            assert lease is not None and lease.status == "active"
            with pytest.raises(RuntimeError, match="Turn 已存在执行 scope"):
                registry.create(stream_id)
            await registry.close(stream_id)
        assert streams == [stream_id] * (2 if fail_first_write else 1)

    assert registry.get(stream_id) is registry.get_inbox(stream_id) is None
    restored = ExternalResourceLeaseLedger(state_path=case.workspace / ".boxteam/resources.json")
    lease, peer = restored.get_lease(lease_id), restored.get_lease(peer_id)
    assert lease is not None and lease.status == "released" and lease.turn_stream_id == stream_id
    assert peer is not None and peer.status == "active" and peer.turn_stream_id == "test-peer-turn"
    resource = restored.get(resource_id)
    assert resource is not None and resource.status == "running"
    await case.plan(session_id, [{"content": "同一 Thread 后继执行成功"}])
    successor = await _send(case, session_id, "继续执行")
    assert successor["thread_id"] == accepted["thread_id"]
    assert successor["turn_id"] != accepted["turn_id"]
    successor_job_id = successor["job_id"]
    assert isinstance(successor_job_id, str)
    await _job(case, successor_job_id)
    case.write_actual("turn-close-" + str(fail_first_write), {
        "accepted": accepted, "successor": successor, "original_stream_id": stream_id,
        "release_calls": streams, "ledger_writer_threads": writer_threads, "loop_thread": loop_thread,
        "lease_status": lease.status, "peer_status": peer.status, "resource_status": resource.status,
    })
