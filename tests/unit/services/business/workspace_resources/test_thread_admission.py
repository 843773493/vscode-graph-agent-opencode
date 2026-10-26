"""Thread短准入、真实OS holder与资源操作生命周期相互独立。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from app.core.session_lifecycle_gate import OperationHolderGate, SessionLifecycleGate
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import (
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
)
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
def gates(bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch):
    gate = SessionLifecycleGate(bundle.root / ".boxteam" / "sessions")
    original = bundle.sessions.session_write_admission

    @asynccontextmanager
    async def admitting(sid: str):
        async with gate.exclusive(sid), original(sid):
            yield

    monkeypatch.setattr(bundle.sessions, "session_write_admission", admitting)
    return gate


@pytest.fixture
def resource_holder(bundle: TerminalOwnerFixture | BrowserOwnerFixture) -> OperationHolderGate:
    # 独立打开原namespace的OS锁核验产品持有期，不由测试为owner补装holder。
    return OperationHolderGate(bundle.root / ".boxteam" / "sessions", namespace="workspace_resources")


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    original = asyncio.timeout
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        context = original(seconds)
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() == "thread-admit-under-test":
            assert seconds == 30
            captured.append(context)
        return context

    monkeypatch.setattr(asyncio, "timeout", capture)
    return captured


@pytest.mark.parametrize("stage", ["holder", "session", "read"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_drains_only_this_admission(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, gates, resource_holder, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, stage: str, cancel: bool,
) -> None:
    await bundle.create()
    owner = bundle.owner
    assert owner is not None
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    bodies: list[str] = []
    get = bundle.sessions.get

    async def reading(sid: str):
        result = await get(sid)
        if sid == SESSION_A and stage == "read":
            entered.set()
            try:
                await release.wait()
            finally:
                drained.set()
        return result

    async def access(sid: str) -> None:
        async with owner.operation(session_id=sid, thread_id=THREAD_A if sid == SESSION_A else THREAD_B,
                                   holder_id="request:thread-admit", operation_id="thread-admit"):
            bodies.append(sid)

    with monkeypatch.context() as patch:
        patch.setattr(bundle.sessions, "get", reading)
        async with AsyncExitStack() as holders:
            held = None
            if stage == "holder":
                held = await holders.enter_async_context(resource_holder.lock_for(SESSION_A))
            elif stage == "session":
                held = await holders.enter_async_context(gates.exclusive(SESSION_A))
            before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
            pending = asyncio.create_task(access(SESSION_A), name="thread-admit-under-test")
            try:
                if stage == "read":
                    await asyncio.wait_for(entered.wait(), 1)
                else:
                    await asyncio.sleep(0)
                    assert not pending.done()
                await asyncio.wait_for(access(SESSION_B), 1)
                if cancel:
                    pending.cancel("原Thread准入取消")
                    with pytest.raises(asyncio.CancelledError, match="原Thread准入取消"):
                        await pending
                else:
                    if deadlines:
                        deadline = deadlines[0].when()
                        assert deadline is not None
                        deadlines[0].reschedule(deadline - 31)
                    with pytest.raises(TimeoutError, match="Thread 准入等待超过 30 秒") as caught:
                        await asyncio.wait_for(asyncio.shield(pending), 0.3)
                    assert len(deadlines) == 1 and deadlines[0].expired()
                    assert SESSION_A in str(caught.value) and str(bundle.root) not in str(caught.value)
                assert owner._store.path.read_bytes() == before and ledger_path.read_bytes() == ledger
                assert bodies == [SESSION_B]
                if held is not None:
                    assert held.acquired
                if stage == "read":
                    assert drained.is_set()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                release.set()
        await asyncio.sleep(0)
        assert bodies == [SESSION_B]
    async with resource_holder.lock_for(SESSION_A, wait=False) as proof:
        assert proof.acquired
    await asyncio.wait_for(access(SESSION_A), 1)
    assert bodies == [SESSION_B, SESSION_A]


async def test_session_wait_and_read_share_one_budget(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, gates, resource_holder, deadlines: list[asyncio.Timeout],
    advance_clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    await owner.recover(())
    entered, release = asyncio.Event(), asyncio.Event()
    get = bundle.sessions.get

    async def reading(sid: str):
        result = await get(sid)
        entered.set()
        await release.wait()
        return result

    async def access() -> None:
        async with owner.admit_thread(SESSION_A, THREAD_A):
            pytest.fail("准入预算已到期，不能进入工具主体")

    lock = gates.exclusive(SESSION_A)
    await lock.__aenter__()
    with monkeypatch.context() as patch:
        patch.setattr(bundle.sessions, "get", reading)
        pending = asyncio.create_task(access(), name="thread-admit-under-test")
        try:
            await asyncio.sleep(0)
            advance_clock(20)
            await lock.__aexit__(None, None, None)
            await asyncio.wait_for(entered.wait(), 1)
            advance_clock(11)
            with pytest.raises(TimeoutError, match="Thread 准入等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 0.3)
            assert len(deadlines) == 1 and deadlines[0].expired()
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await lock.__aexit__(None, None, None)
    async with resource_holder.lock_for(SESSION_A, wait=False) as proof:
        assert proof.acquired


async def test_body_keeps_holder_but_releases_session_gate(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, gates, resource_holder,
    deadlines: list[asyncio.Timeout], advance_clock,
) -> None:
    owner = bundle.owner
    assert owner is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def operation() -> None:
        async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                                   holder_id="request:long", operation_id="long"):
            entered.set()
            await release.wait()

    pending = asyncio.create_task(operation(), name="thread-admit-under-test")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        advance_clock(31)
        await asyncio.sleep(0)
        assert not pending.done()

        async def peer() -> None:
            async with owner.admit_thread(SESSION_A, THREAD_A) as ref:
                assert ref.session_id == SESSION_A
        await asyncio.wait_for(peer(), 1)
        assert len(deadlines) == 1 and not deadlines[0].expired()
        async with resource_holder.lock_for(SESSION_A, wait=False) as proof:
            assert not proof.acquired
    finally:
        release.set()
        await asyncio.wait_for(pending, 1)
    async with resource_holder.lock_for(SESSION_A, wait=False) as proof:
        assert proof.acquired


@pytest.mark.parametrize("error", [TimeoutError("原Session读取超时"), KeyError("原Thread身份错误")])
async def test_read_failure_keeps_identity_and_releases_holder(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, gates, resource_holder, monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    owner = bundle.owner
    assert owner is not None

    async def failing(_sid: str):
        raise error

    monkeypatch.setattr(bundle.sessions, "get", failing)
    with pytest.raises(type(error)) as caught:
        async with owner.admit_thread(SESSION_A, THREAD_A):
            pytest.fail("原异常不能进入工具主体")
    assert caught.value is error
    async with resource_holder.lock_for(SESSION_A, wait=False) as proof:
        assert proof.acquired
