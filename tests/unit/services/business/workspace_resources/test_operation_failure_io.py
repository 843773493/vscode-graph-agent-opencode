"""工具失败的原账本及owner收尾不阻塞事件循环，也不被后续取消割裂。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, get_ident

import pytest

from app.services.business.terminal_resources.operations import TerminalOperations
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    LeasedOperation,
    ReleaseWrite,
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    leased_operation as leased_operation,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    release_write as release_write,  # noqa: PLC0414
)

pytestmark = pytest.mark.parametrize("release_write", ["receipt"], indirect=True)


@dataclass
class FailedCall:
    operation: LeasedOperation
    original: BaseException
    dispatches: int = 0
    before_failure: Callable[[], Awaitable[None]] | None = None
    failed: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        operation, rid, _ = self.operation
        if isinstance(operation, TerminalOperations):
            await operation.read_terminal(rid)
        else:
            await operation.perform(rid, "read", {})


@pytest.fixture(params=["failure", "cancel"])
async def failed_call(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation, request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FailedCall]:
    original = RuntimeError("原工具回执丢失") if request.param == "failure" else asyncio.CancelledError("原工具取消")
    case = FailedCall(leased_operation, original)

    async def fail(*args, **kwargs):
        case.dispatches += 1
        if case.before_failure is not None:
            await case.before_failure()
        case.failed.set()
        raise original

    method = "read_terminal" if isinstance(leased_operation[0], TerminalOperations) else "perform"
    monkeypatch.setattr(bundle.transport, method, fail)
    yield case


@pytest.mark.parametrize("cancel_cleanup", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_failed_operation_settles_original_unknown_writer_before_unlocking(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, failed_call: FailedCall,
    release_write: ReleaseWrite, cancel_cleanup: bool, write_fails: bool,
) -> None:
    operation, rid, lease = failed_call.operation
    owner, path, gate = bundle.owner, bundle.leases._state_path, release_write
    assert owner is not None and path is not None
    before, records = path.read_bytes(), owner._store.path.read_bytes()
    gate.failure = OSError(28, "unknown账本原生写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(failed_call.run())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert gate.before is not None and gate.lease is not None
        before, inflight = gate.before, gate.lease
        assert not pending.done(), "失败收尾不能阻塞事件循环"
        assert len(gate.threads) == 1 and gate.threads[0] != get_ident()
        assert owner._resource_lock(rid).locked() and failed_call.dispatches == 1
        assert path.read_bytes() == before and owner._store.path.read_bytes() == records
        assert type(bundle.leases)(state_path=path).get_lease(lease.lease_id) == inflight
        if cancel_cleanup:
            pending.cancel("后续取消不能割裂原失败收尾")
            await asyncio.sleep(0)
            pending.cancel("重复取消不能丢弃原错误")
            await asyncio.sleep(0)
            assert not pending.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(OSError if write_fails else type(failed_call.original)) as caught:
            await pending
        if write_fails:
            assert caught.value.__cause__ is gate.failure
            assert caught.value.__context__ is failed_call.original
            assert path.read_bytes() == before and owner._store.path.read_bytes() == records
            assert type(bundle.leases)(state_path=path).get_lease(lease.lease_id) == inflight
            # 只在显式重试成功后声明占用已保护；失败不能冒充unknown持久成功。
            await owner._unknown(owner._require_owned_resource_record(rid), failed_call.original, lease_id=lease.lease_id)
        else:
            assert caught.value is failed_call.original
        assert not owner._resource_lock(rid).locked() and failed_call.dispatches == 1
        protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert protected is not None and protected.status == "reconcile_required"
        assert (protected.resource_id, protected.operation_id, protected.holder_thread_id) == (
            rid, lease.operation_id, lease.holder_thread_id,
        )
        record = type(owner._store)(workspace_root=bundle.root, workspace_id=owner.workspace_id, kind=owner.kind).get(rid)
        assert record is not None and record.reconciliation_status == "unknown"
        assert record.error is not None and f"error={type(failed_call.original).__name__}" in record.error
        await operation.close()
        assert operation._lease_refs == {} and bundle.leases.get_lease(lease.lease_id) == protected
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_queued_unknown_cleanup_survives_cancellation_and_preserves_original_error(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, failed_call: FailedCall,
    release_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, rid, lease = failed_call.operation
    owner, path, gate = bundle.owner, bundle.leases._state_path, release_write
    assert owner is not None and path is not None
    loop, occupied, free = asyncio.get_running_loop(), asyncio.Event(), Event()

    def occupying() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free.wait(5), "测试未释放原线程池占用"

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        occupying_task: asyncio.Future[None] | None = None

        async def occupy_after_dispatch() -> None:
            nonlocal occupying_task
            occupying_task = loop.run_in_executor(None, occupying)
            await occupied.wait()

        failed_call.before_failure = occupy_after_dispatch
        records = owner._store.path.read_bytes()
        pending = asyncio.create_task(failed_call.run())
        try:
            await asyncio.wait_for(failed_call.failed.wait(), 2)
            assert gate.before is not None
            before = gate.before
            pending.cancel("排队时取消失败收尾")
            await asyncio.sleep(0)
            pending.cancel("再次取消不能跳过owner记录")
            await asyncio.sleep(0)
            assert not pending.done() and owner._resource_lock(rid).locked()
            assert gate.threads == [] and path.read_bytes() == before
            assert owner._store.path.read_bytes() == records
            free.set()
            await asyncio.wait_for(gate.entered.wait(), 2)
            assert not pending.done() and owner._resource_lock(rid).locked()
            gate.release.set()
            with pytest.raises(type(failed_call.original)) as caught:
                await pending
            assert caught.value is failed_call.original
            record = type(owner._store)(workspace_root=bundle.root, workspace_id=owner.workspace_id, kind=owner.kind).get(rid)
            assert record is not None and record.reconciliation_status == "unknown"
            protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
            assert protected is not None and protected.status == "reconcile_required"
            await operation.close()
            assert bundle.leases.get_lease(lease.lease_id) == protected
            assert failed_call.dispatches == 1 and len(gate.threads) == 1
        finally:
            free.set()
            gate.release.set()
            waiting = [pending] if occupying_task is None else [pending, occupying_task]
            await asyncio.gather(*waiting, return_exceptions=True)
