"""operation 收尾沿原账本写入，原资源锁覆盖线程排队和取消结算。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, get_ident
from time import monotonic

import pytest

from app.core.session_lifecycle_gate import OperationHolderGate
from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import operations as resource_operations
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)

LeasedOperation = tuple[TerminalOperations | BrowserOperations, str, ResourceLease]


@dataclass
class ReleaseWrite:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    threads: list[int] = field(default_factory=list)
    failure: OSError | None = None
    enabled: bool = True
    before: bytes | None = None
    lease: ResourceLease | None = None


@pytest.fixture
async def leased_operation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
) -> AsyncIterator[LeasedOperation]:
    owner = bundle.owner
    assert owner is not None
    rid = str((await bundle.create())["resource_id"])
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="request:native-release", operation_id="native-release",
    ) as operation:
        if isinstance(operation, TerminalOperations):
            await operation.read_terminal(rid)
        else:
            await operation.perform(rid, "navigate", {"url": "https://release.test"})
        lease, = bundle.leases.leases_for_resource(rid)
        yield operation, rid, lease


def install_release_write(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> ReleaseWrite:
    assert bundle.leases._state_path is not None
    pending = bundle.leases._state_path.with_suffix(".tmp")
    loop = asyncio.get_running_loop()
    original = Path.replace
    gate = ReleaseWrite()

    def publishing(path: Path, target: Path) -> Path:
        if path == pending and gate.enabled:
            gate.threads.append(get_ident())
            if len(gate.threads) == 1:
                loop.call_soon_threadsafe(gate.entered.set)
                if not gate.release.wait(3):
                    raise TimeoutError("测试未放行 lease 原生写入，事件循环可能被阻塞")
                if gate.failure is not None:
                    raise gate.failure
        return original(path, target)

    monkeypatch.setattr(Path, "replace", publishing)
    return gate


@pytest.fixture
async def release_write(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> AsyncIterator[ReleaseWrite]:
    gate = install_release_write(bundle, monkeypatch)
    if getattr(request, "param", None) == "receipt":
        mark = bundle.leases.mark_operation_dispatched

        def preparing(lease_id: str) -> None:
            # 这些用例注入回执后的写盘；派发准备仍沿真实账本完成，不拦截另一个阶段。
            gate.enabled = False
            try:
                mark(lease_id)
            finally:
                gate.enabled = True
            assert bundle.leases._state_path is not None
            gate.before = bundle.leases._state_path.read_bytes()
            gate.lease = bundle.leases.get_lease(lease_id)

        monkeypatch.setattr(bundle.leases, "mark_operation_dispatched", preparing)
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.fixture
async def operation_exit_write(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ReleaseWrite]:
    gate = install_release_write(bundle, monkeypatch)
    gate.enabled = False
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.mark.parametrize("write_fails", [False, True])
async def test_operation_release_keeps_loop_responsive_until_native_commit(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation,
    release_write: ReleaseWrite, write_fails: bool,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    before = path.read_bytes()
    native = OSError(28, "原 lease 写盘失败", str(path))
    release_write.failure = native if write_fails else None
    closing = asyncio.create_task(operation.close())
    try:
        await asyncio.wait_for(release_write.entered.wait(), 2)
        assert not closing.done(), "原生写盘结束前，事件循环必须能继续执行"
        assert len(release_write.threads) == 1
        assert release_write.threads[0] != get_ident()
        assert owner._resource_lock(rid).locked()
        assert path.read_bytes() == before and operation._lease_refs == {rid: lease.lease_id}
        assert bundle.leases.get_lease(lease.lease_id) == lease
        assert type(bundle.leases)(state_path=path).get_lease(lease.lease_id) == lease
        release_write.release.set()
        if write_fails:
            with pytest.raises(OSError) as caught:
                await closing
            assert caught.value.__cause__ is native and caught.value.errno == 28
            assert str(path) not in str(caught.value)
            assert path.read_bytes() == before and operation._lease_refs == {rid: lease.lease_id}
            assert bundle.leases.get_lease(lease.lease_id) == lease
        else:
            await closing
            assert operation._lease_refs == {}
        assert not owner._resource_lock(rid).locked()
        await operation.close()
        assert operation._lease_refs == {}
        released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert released is not None and released.status == "released"
        assert released.resource_id == rid and released.operation_id == lease.operation_id
        assert len(release_write.threads) == (2 if write_fails else 1)
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        release_write.release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_interrupted_close_observes_boundedly_and_reuses_original_writer(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation,
    release_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    interruption: str, write_fails: bool,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    before = path.read_bytes()
    native = OSError(28, "取消期间原 lease 写盘失败", str(path))
    release_write.failure = native if write_fails else None
    monkeypatch.setattr(resource_operations, "OPERATION_CLOSE_WAIT_SECONDS", 0.05)
    began = monotonic()
    closing = asyncio.create_task(operation.close())
    observing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(release_write.entered.wait(), 2)
        assert not closing.done(), "原生写盘不能阻塞取消与期限观察"
        if interruption == "cancel":
            closing.cancel("原 operation 取消")
            await asyncio.sleep(0)
            closing.cancel("再次取消不能遗弃原 writer")
        expected = asyncio.CancelledError if interruption == "cancel" else TimeoutError
        done, _ = await asyncio.wait((closing,), timeout=1)
        assert closing in done, "原生写盘未结束时，本次观察不能继续等待原 batch"
        with pytest.raises(expected) as interrupted:
            await closing
        assert monotonic() - began < 1, "原生写盘尚未放行，本次关闭观察必须有界返回"
        assert (interrupted.value.args == ("原 operation 取消",) if interruption == "cancel"
                else "总截止时间" in str(interrupted.value))
        assert interrupted.value.__cause__ is None
        batch, tasks = operation._close_task, operation._close_tasks
        assert batch is not None and not batch.done() and not batch.cancelled()
        assert owner._resource_lock(rid).locked()
        assert path.read_bytes() == before and operation._lease_refs == {rid: lease.lease_id}
        assert bundle.leases.get_lease(lease.lease_id) == lease
        assert type(bundle.leases)(state_path=path).get_lease(lease.lease_id) == lease
        observing = asyncio.create_task(operation.close())
        await asyncio.sleep(0)
        assert operation._close_task is batch and operation._close_tasks is tasks
        assert len(release_write.threads) == 1
        release_write.release.set()
        if write_fails:
            with pytest.raises(OSError) as failed:
                await observing
            assert failed.value.__cause__ is native
            assert any(record.exc_info and record.exc_info[1] is failed.value for record in caplog.records)
        else:
            await observing
        assert operation._lease_refs == ({rid: lease.lease_id} if write_fails else {})
        assert not owner._resource_lock(rid).locked()
        saved = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert saved is not None and saved.status == ("active" if write_fails else "released")
        await operation.close()
        await operation.close()
        assert operation._lease_refs == {}
        saved = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert saved is not None and saved.status == "released"
        assert len(release_write.threads) == (2 if write_fails else 1)
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        release_write.release.set()
        await asyncio.gather(closing, *(() if observing is None else (observing,)), return_exceptions=True)


async def test_release_queue_cancellation_keeps_original_lock_and_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation,
    release_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    loop = asyncio.get_running_loop()
    occupied, free_worker = asyncio.Event(), Event()

    def occupying() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free_worker.wait(5), "测试未释放原线程池占用"

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        occupying_task = loop.run_in_executor(None, occupying)
        await asyncio.wait_for(occupied.wait(), 2)
        before = path.read_bytes()
        closing = asyncio.create_task(operation.close())
        try:
            async with asyncio.timeout(2):
                while not owner._resource_lock(rid).locked():
                    await asyncio.sleep(0)
            closing.cancel("原线程池排队取消")
            await asyncio.sleep(0)
            closing.cancel("排队再次取消")
            done, _ = await asyncio.wait((closing,), timeout=1)
            assert closing in done
            with pytest.raises(asyncio.CancelledError, match="原线程池排队取消"):
                await closing
            batch = operation._close_task
            assert batch is not None and not batch.done() and not batch.cancelled()
            assert owner._resource_lock(rid).locked()
            assert release_write.threads == [] and path.read_bytes() == before
            assert bundle.leases.get_lease(lease.lease_id) == lease
            free_worker.set()
            await asyncio.wait_for(release_write.entered.wait(), 2)
            assert not batch.done() and owner._resource_lock(rid).locked()
            release_write.release.set()
            await operation.close()
            assert operation._lease_refs == {}
            assert not owner._resource_lock(rid).locked()
            await operation.close()
            assert operation._lease_refs == {} and len(release_write.threads) == 1
        finally:
            free_worker.set()
            release_write.release.set()
            await asyncio.gather(closing, occupying_task, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
async def test_shared_close_batch_keeps_prior_error_and_does_not_resume_cancelled_lock_waits(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    operation_exit_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    owner = bundle.owner
    assert owner is not None
    resources = [str((await bundle.create())["resource_id"]) for _ in range(3)]
    failed, writing, locked = resources
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="request:mixed-close", operation_id="mixed-close",
    ) as operation:
        for rid in resources:
            if isinstance(operation, TerminalOperations):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "navigate", {"url": "https://mixed-close.test"})
        leases = {rid: bundle.leases.leases_for_resource(rid)[0] for rid in resources}
        ledger_ids = set(bundle.leases._leases)
        original = TimeoutError("原账本自己的释放超时")
        releasing = owner._release_operation_lease
        calls: list[str] = []
        should_fail = True

        def release(lease_id: str) -> None:
            calls.append(lease_id)
            if lease_id == leases[failed].lease_id and should_fail:
                raise original
            releasing(lease_id)

        monkeypatch.setattr(owner, "_release_operation_lease", release)
        monkeypatch.setattr(resource_operations, "OPERATION_CLOSE_WAIT_SECONDS", 0.05)
        operation_exit_write.enabled = True
        lock = owner._resource_lock(locked)
        await lock.acquire()
        closing = asyncio.create_task(operation.close())
        observing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(operation_exit_write.entered.wait(), 2)
            async with asyncio.timeout(1):
                while not operation._close_tasks[failed].done():
                    await asyncio.sleep(0)
            batch, tasks = operation._close_task, operation._close_tasks
            monkeypatch.setattr(resource_operations, "OPERATION_CLOSE_WAIT_SECONDS", 1)
            observing = asyncio.create_task(operation.close())
            await asyncio.sleep(0)
            assert operation._close_task is batch and operation._close_tasks is tasks
            if interruption == "cancel":
                closing.cancel("整批观察原取消")
            done, _ = await asyncio.wait((closing,), timeout=1)
            assert closing in done
            if interruption == "cancel":
                with pytest.raises(asyncio.CancelledError, match="整批观察原取消") as cancelled:
                    await closing
                assert isinstance(cancelled.value.__cause__, BaseExceptionGroup)
                assert cancelled.value.__cause__.exceptions == (original,)
            else:
                with pytest.raises(ExceptionGroup) as expired:
                    await closing
                assert expired.value.exceptions[0] is original
                assert len(expired.value.exceptions) == 2
                assert isinstance(expired.value.exceptions[1], TimeoutError)
                assert "总截止时间" in str(expired.value.exceptions[1])
            assert batch is not None and not batch.done() and not batch.cancelled()
            assert not lock._waiters and not observing.done()
            lock.release()
            await asyncio.sleep(0)
            assert operation._lease_refs == {rid: lease.lease_id for rid, lease in leases.items()}
            assert calls.count(leases[writing].lease_id) == 1 and leases[locked].lease_id not in calls
            operation_exit_write.release.set()
            with pytest.raises(ExceptionGroup) as shared:
                await observing
            assert shared.value.exceptions[0] is original
            assert len(shared.value.exceptions) == 2
            assert "锁等待已中断" in str(shared.value.exceptions[1])
            assert operation._lease_refs == {rid: leases[rid].lease_id for rid in (failed, locked)}
            should_fail = False
            await operation.close()
            assert operation._lease_refs == {} and set(bundle.leases._leases) == ledger_ids
            assert [calls.count(leases[rid].lease_id) for rid in resources] == [2, 1, 1]
            for lease in leases.values():
                released = bundle.leases.get_lease(lease.lease_id)
                assert released is not None and released.status == "released"
        finally:
            operation_exit_write.release.set()
            if lock.locked():
                lock.release()
            await asyncio.gather(closing, *(() if observing is None else (observing,)), return_exceptions=True)


@pytest.mark.parametrize("ending", ["success", "close-cancel", "deadline", "body-cancel", "body-error"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_owner_operation_exit_preserves_original_outcome_and_holder_until_release(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    operation_exit_write: ReleaseWrite, ending: str, write_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    operation_exit_write.enabled = False
    rid = str((await bundle.create())["resource_id"])
    instances: list[TerminalOperations | BrowserOperations] = []
    continued: list[bool] = []
    original = (asyncio.CancelledError("operation主体原取消") if ending == "body-cancel"
                else RuntimeError("operation主体原错误") if ending == "body-error" else None)
    native = OSError(28, "operation退出原账本写失败", str(path))
    operation_exit_write.failure = native if write_fails else None
    holder = OperationHolderGate(bundle.root / ".boxteam" / "sessions", namespace="workspace_resources")
    if ending == "deadline":
        monkeypatch.setattr(resource_operations, "OPERATION_CLOSE_WAIT_SECONDS", 0.05)

    async def invoke() -> None:
        async with owner.operation(
            session_id=SESSION_A, thread_id=THREAD_A,
            holder_id="request:owner-exit", operation_id="owner-exit",
        ) as operation:
            instances.append(operation)
            if isinstance(operation, TerminalOperations):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "navigate", {"url": "https://exit.test"})
            operation_exit_write.enabled = True
            if original is not None:
                raise original
        continued.append(True)

    pending = asyncio.create_task(invoke())
    deleting: asyncio.Task[None] | None = None
    deleting_entered = asyncio.Event()
    try:
        await asyncio.wait_for(operation_exit_write.entered.wait(), 2)
        operation, = instances
        lease, = bundle.leases.leases_for_resource(rid)
        before = path.read_bytes()
        assert lease.status == "active" and lease.operation_id == "owner-exit"
        assert operation._closed and owner._resource_lock(rid).locked()
        assert operation_exit_write.threads and all(thread != get_ident() for thread in operation_exit_write.threads)
        async with holder.lock_for(SESSION_A, wait=False) as proof:
            assert not proof.acquired, "原Thread holder须覆盖operation退出写盘"
        if ending == "close-cancel":
            pending.cancel("operation正常退出首次取消")
        returned_early = ending in {"close-cancel", "deadline"}
        if returned_early:
            expected = asyncio.CancelledError if ending == "close-cancel" else TimeoutError
            done, _ = await asyncio.wait((pending,), timeout=1)
            assert pending in done, "正式 owner 入口必须在原生写盘结算前结束本次关闭观察"
            with pytest.raises(expected) as interrupted:
                await pending
            assert (interrupted.value.args == ("operation正常退出首次取消",) if ending == "close-cancel"
                    else "总截止时间" in str(interrupted.value))
            assert interrupted.value.__cause__ is None
        elif ending != "success":
            for _ in range(5):
                await asyncio.sleep(0)
            pending.cancel("收尾重复取消不得替换原结果")
            await asyncio.sleep(0)
        assert pending.done() is returned_early
        assert owner._resource_lock(rid).locked()
        assert path.read_bytes() == before and continued == []
        async with holder.lock_for(SESSION_A, wait=False) as proof:
            assert not proof.acquired
        if returned_early:
            async def deletion_holder() -> None:
                # Session 删除取得同一 namespace 的 exclusive；原调用已返回也不得越过写盘。
                async with holder.lock_for(SESSION_A):
                    deleting_entered.set()

            deleting = asyncio.create_task(deletion_holder())
            await asyncio.sleep(0)
            assert not deleting.done() and not deleting_entered.is_set()
        operation_exit_write.release.set()
        if returned_early:
            assert operation._close_task is not None
            results = await operation._close_task
            failure, = results
            assert deleting is not None
            await asyncio.wait_for(deleting, 1)
            assert deleting_entered.is_set()
        elif original is not None:
            with pytest.raises(type(original)) as caught:
                await pending
            assert caught.value is original
            failure = caught.value.__cause__
        elif write_fails:
            with pytest.raises(OSError) as failed:
                await pending
            failure = failed.value
        else:
            await pending
            failure = None
        if write_fails:
            assert isinstance(failure, OSError) and failure.__cause__ is native
            assert operation._lease_refs == {rid: lease.lease_id} and path.read_bytes() == before
        else:
            assert failure is None and operation._lease_refs == {}
        assert continued == ([True] if ending == "success" and not write_fails else [])
        assert not owner._resource_lock(rid).locked()
        async with holder.lock_for(SESSION_A, wait=False) as proof:
            assert proof.acquired
        await operation.close()
        released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert released is not None and released.status == "released"
        assert released == replace(lease, status="released", updated_at=released.updated_at)
        assert operation._lease_refs == {} and len(operation_exit_write.threads) == (2 if write_fails else 1)
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        operation_exit_write.release.set()
        await asyncio.gather(pending, *(() if deleting is None else (deleting,)), return_exceptions=True)
