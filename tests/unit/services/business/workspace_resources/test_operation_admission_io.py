"""lease 准入写盘与关闭共用原任务，取消不能遗忘已提交的占用。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)


@dataclass
class AdmissionCase:
    operation: TerminalOperations | BrowserOperations
    rid: str
    dispatches: list[str] = field(default_factory=list)

    async def read(self) -> None:
        if isinstance(self.operation, TerminalOperations):
            await self.operation.read_terminal(self.rid)
        else:
            await self.operation.perform(self.rid, "read", {})


@dataclass
class AdmissionWrite:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    threads: list[int] = field(default_factory=list)
    failure: OSError | None = None


@pytest.fixture
async def admission_case(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AdmissionCase]:
    owner = bundle.owner
    assert owner is not None
    rid = str((await bundle.create())["resource_id"])
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:admission-io", operation_id="admission-io") as operation:
        case = AdmissionCase(operation, rid)
        method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
        dispatch = getattr(bundle.transport, method)

        async def observed(resource_id: str, *args, **kwargs):
            case.dispatches.append(resource_id)
            return await dispatch(resource_id, *args, **kwargs)

        monkeypatch.setattr(bundle.transport, method, observed)
        yield case


@pytest.fixture
async def admission_write(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, admission_case: AdmissionCase,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AdmissionWrite]:
    path = bundle.leases._state_path
    assert path is not None
    pending = path.with_suffix(".tmp")
    loop, replace = asyncio.get_running_loop(), Path.replace
    gate = AdmissionWrite()

    def publishing(source: Path, target: Path) -> Path:
        if source == pending:
            gate.threads.append(get_ident())
            if len(gate.threads) == 1:
                loop.call_soon_threadsafe(gate.entered.set)
                if not gate.release.wait(3):
                    raise TimeoutError("测试未放行 lease 准入写盘，事件循环可能被阻塞")
                if gate.failure is not None:
                    raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publishing)
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.mark.parametrize("write_fails", [False, True])
async def test_admission_write_keeps_loop_responsive_and_publishes_before_dispatch(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, admission_case: AdmissionCase,
    admission_write: AdmissionWrite, write_fails: bool,
) -> None:
    case, gate, owner = admission_case, admission_write, bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    before, records = path.read_bytes(), owner._store.path.read_bytes()
    gate.failure = OSError(28, "原准入写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(case.read())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原准入写盘不能阻塞事件循环"
        assert gate.threads == [gate.threads[0]] and gate.threads[0] != get_ident()
        assert owner._resource_lock(case.rid).locked() and case.dispatches == []
        assert case.operation._lease_refs == {} and path.read_bytes() == before
        assert owner._store.path.read_bytes() == records
        assert not ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
        gate.release.set()
        if write_fails:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value.__cause__ is gate.failure and caught.value.errno == 28
            assert str(path) not in str(caught.value)
            assert case.operation._lease_refs == {} and path.read_bytes() == before
            assert case.dispatches == []
        else:
            await pending
            assert case.dispatches == [case.rid]
        await case.read()
        lease, = bundle.leases.leases_for_resource(case.rid)
        assert case.operation._lease_refs == {case.rid: lease.lease_id}
        assert lease.status == "active" and lease.operation_id == "admission-io"
        assert ExternalResourceLeaseLedger(state_path=path).get_lease(lease.lease_id) == lease
        assert len(gate.threads) == (2 if write_fails else 1)
        assert case.dispatches == [case.rid] * (1 if write_fails else 2)
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
@pytest.mark.parametrize("failure", ["none", "io", "timeout"])
async def test_interrupted_admission_keeps_original_writer_and_precise_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, admission_case: AdmissionCase,
    admission_write: AdmissionWrite, advance_clock: Callable[[float], None],
    interruption: str, failure: str,
) -> None:
    case, gate, owner = admission_case, admission_write, bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    gate.failure = (OSError(28, "原生写盘失败", str(path)) if failure == "io" else
                    TimeoutError("原生存储自己的超时") if failure == "timeout" else None)
    before = path.read_bytes()
    pending = asyncio.create_task(case.read())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "取消必须能在原生写盘未结束时被观察"
        if interruption == "cancel":
            pending.cancel("原准入取消")
        else:
            advance_clock(31)
        for _ in range(5):
            await asyncio.sleep(0)
        if interruption == "cancel":
            pending.cancel("再次取消不能遗忘原 writer")
            await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(case.rid).locked()
        assert case.dispatches == [] and path.read_bytes() == before
        gate.release.set()
        with pytest.raises(asyncio.CancelledError if interruption == "cancel" else TimeoutError) as caught:
            await pending
        if interruption == "cancel":
            cancellation = caught.value
        else:
            budget_error = caught.value.__cause__
            assert isinstance(budget_error, TimeoutError)
            cancellation = budget_error.__cause__
        assert isinstance(cancellation, asyncio.CancelledError)
        if interruption == "cancel":
            assert str(cancellation) == "原准入取消"
        else:
            assert "operation 准入等待超过 30 秒" in str(caught.value)
        if gate.failure is not None:
            io_error = cancellation.__cause__
            assert isinstance(io_error, OSError) and io_error.__cause__ is gate.failure
            assert path.read_bytes() == before and case.operation._lease_refs == {}
            assert not ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
        else:
            lease, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
            assert lease.status == "active" and case.operation._lease_refs == {case.rid: lease.lease_id}
        assert case.dispatches == [] and not owner._resource_lock(case.rid).locked()
        await case.operation.close()
        assert case.operation._lease_refs == {} and not bundle.leases.leases_for_resource(case.rid)
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "deadline", "close"])
async def test_queued_admission_does_not_start_new_write_after_interruption(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, admission_case: AdmissionCase,
    admission_write: AdmissionWrite, advance_clock: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    case, gate, owner = admission_case, admission_write, bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    loop, occupied, free = asyncio.get_running_loop(), asyncio.Event(), Event()

    def occupying() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free.wait(5), "测试未释放原线程池占用"

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        occupying_task = loop.run_in_executor(None, occupying)
        await asyncio.wait_for(occupied.wait(), 2)
        before = path.read_bytes()
        pending = asyncio.create_task(case.read())
        closing: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(2):
                while not owner._resource_lock(case.rid).locked():
                    await asyncio.sleep(0)
            if interruption == "close":
                closing = asyncio.create_task(case.operation.close())
            elif interruption == "cancel":
                pending.cancel("原排队取消")
            else:
                advance_clock(31)
            for _ in range(5):
                await asyncio.sleep(0)
            assert not pending.done() and owner._resource_lock(case.rid).locked()
            if closing is not None:
                assert not closing.done(), "关闭不能遗漏仍在线程池排队的准入"
            assert gate.threads == [] and path.read_bytes() == before and case.dispatches == []
            free.set()
            expected = RuntimeError if interruption == "close" else asyncio.CancelledError if interruption == "cancel" else TimeoutError
            with pytest.raises(expected):
                await pending
            if closing is not None:
                await closing
            assert gate.threads == [] and path.read_bytes() == before
            assert case.operation._lease_refs == {} and case.dispatches == []
            assert not owner._resource_lock(case.rid).locked()
            assert not ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
        finally:
            free.set()
            gate.release.set()
            await asyncio.gather(pending, occupying_task, *([] if closing is None else [closing]), return_exceptions=True)


@pytest.mark.parametrize("cancel_close", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_close_tracks_acquisition_before_lease_is_published(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, admission_case: AdmissionCase,
    admission_write: AdmissionWrite, cancel_close: bool, write_fails: bool,
) -> None:
    case, gate, owner = admission_case, admission_write, bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    gate.failure = OSError(28, "关闭期间原准入写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(case.read())
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原准入写盘期间仍能关闭 operation"
        closing = asyncio.create_task(case.operation.close())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not closing.done(), "关闭必须等待已经开始但尚无 lease 引用的原准入"
        if cancel_close:
            closing.cancel("原关闭取消")
            with pytest.raises(asyncio.CancelledError, match="原关闭取消"):
                await closing
            assert not pending.done() and owner._resource_lock(case.rid).locked()
        gate.release.set()
        with pytest.raises(OSError if write_fails else RuntimeError) as caught:
            await pending
        if write_fails:
            assert caught.value.__cause__ is gate.failure
        else:
            assert "operation 已关闭" in str(caught.value)
        if not cancel_close:
            await closing
        assert case.dispatches == []
        if cancel_close and not write_fails:
            lease, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
            assert case.operation._lease_refs == {case.rid: lease.lease_id}
        await case.operation.close()
        assert case.operation._lease_refs == {} and not bundle.leases.leases_for_resource(case.rid)
        assert not ExternalResourceLeaseLedger(state_path=path).leases_for_resource(case.rid)
        assert not owner._resource_lock(case.rid).locked()
    finally:
        gate.release.set()
        await asyncio.gather(pending, *([] if closing is None else [closing]), return_exceptions=True)
