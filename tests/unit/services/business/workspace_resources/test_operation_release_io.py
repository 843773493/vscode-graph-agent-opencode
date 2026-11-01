"""operation 收尾沿原账本写入，原资源锁覆盖线程排队和取消结算。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.core.session_lifecycle_gate import OperationHolderGate
from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
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
async def test_interrupted_release_settles_original_writer_before_unlocking(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    leased_operation: LeasedOperation,
    release_write: ReleaseWrite, advance_clock: Callable[[float], None],
    interruption: str, write_fails: bool,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    before = path.read_bytes()
    native = OSError(28, "取消期间原 lease 写盘失败", str(path))
    release_write.failure = native if write_fails else None
    closing = asyncio.create_task(operation.close())
    try:
        await asyncio.wait_for(release_write.entered.wait(), 2)
        assert not closing.done(), "原生写盘不能阻塞取消与期限观察"
        if interruption == "cancel":
            closing.cancel("原 operation 取消")
        else:
            advance_clock(31)
        for _ in range(5):
            await asyncio.sleep(0)
        if interruption == "cancel":
            closing.cancel("再次取消不能遗弃原 writer")
            await asyncio.sleep(0)
        assert not closing.done() and owner._resource_lock(rid).locked()
        assert path.read_bytes() == before and operation._lease_refs == {rid: lease.lease_id}
        assert bundle.leases.get_lease(lease.lease_id) == lease
        release_write.release.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError, match="原 operation 取消") as cancelled:
                await closing
            errors = cancelled.value.__cause__.exceptions if isinstance(cancelled.value.__cause__, BaseExceptionGroup) else ()
            assert bool(errors) is write_fails
        else:
            with pytest.raises(ExceptionGroup if write_fails else TimeoutError) as expired:
                await closing
            errors = expired.value.exceptions[:-1] if isinstance(expired.value, ExceptionGroup) else ()
            timeout = expired.value.exceptions[-1] if isinstance(expired.value, ExceptionGroup) else expired.value
            assert isinstance(timeout, TimeoutError) and "总截止时间" in str(timeout)
        if write_fails:
            assert len(errors) == 1 and isinstance(errors[0], OSError)
            assert errors[0].__cause__ is native
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
        await asyncio.gather(closing, return_exceptions=True)


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
            await asyncio.sleep(0)
            assert not closing.done() and owner._resource_lock(rid).locked()
            assert release_write.threads == [] and path.read_bytes() == before
            assert bundle.leases.get_lease(lease.lease_id) == lease
            free_worker.set()
            await asyncio.wait_for(release_write.entered.wait(), 2)
            assert not closing.done() and owner._resource_lock(rid).locked()
            release_write.release.set()
            with pytest.raises(asyncio.CancelledError, match="原线程池排队取消"):
                await closing
            assert operation._lease_refs == {}
            assert not owner._resource_lock(rid).locked()
            await operation.close()
            assert operation._lease_refs == {} and len(release_write.threads) == 1
        finally:
            free_worker.set()
            release_write.release.set()
            await asyncio.gather(closing, occupying_task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["success", "close-cancel", "body-cancel", "body-error"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_owner_operation_exit_preserves_original_outcome_and_holder_until_release(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    operation_exit_write: ReleaseWrite, ending: str, write_fails: bool,
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
        if ending != "success":
            for _ in range(5):
                await asyncio.sleep(0)
            pending.cancel("收尾重复取消不得替换原结果")
            await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        assert path.read_bytes() == before and continued == []
        async with holder.lock_for(SESSION_A, wait=False) as proof:
            assert not proof.acquired
        operation_exit_write.release.set()
        if original is not None:
            with pytest.raises(type(original)) as caught:
                await pending
            assert caught.value is original
            failure = caught.value.__cause__
        elif ending == "close-cancel":
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await pending
            assert cancelled.value.args == ("operation正常退出首次取消",)
            errors = cancelled.value.__cause__
            if write_fails:
                assert isinstance(errors, BaseExceptionGroup) and len(errors.exceptions) == 1
                failure = errors.exceptions[0]
            else:
                assert errors is None
                failure = None
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
        await asyncio.gather(pending, return_exceptions=True)
