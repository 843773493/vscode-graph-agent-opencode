"""工具资源准入的唯一预算；到期不代表外部执行已经停止。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.workspace_resources import operations as operations_module
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import CHILD_THREAD_A, SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
def read(bundle: TerminalOwnerFixture | BrowserOwnerFixture):
    async def call(operation, rid: str):
        if isinstance(bundle, TerminalOwnerFixture):
            return await operation.read_terminal(rid)
        return await operation.perform(rid, "read", {})
    return call


@pytest.fixture
def dispatches(bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    method = "read_terminal" if isinstance(bundle, TerminalOwnerFixture) else "perform"
    dispatch = getattr(bundle.transport, method)

    async def observed(rid: str, *args, **kwargs):
        calls.append(rid)
        return await dispatch(rid, *args, **kwargs)

    monkeypatch.setattr(bundle.transport, method, observed)
    return calls


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() == "admit-under-test":
            captured.append(context)
        return context

    # 旧实现没有此观察点；红测应由行为断言失败，而非装具初始化失败。
    monkeypatch.setattr(operations_module, "timeout", capture, raising=False)
    return captured


@pytest.mark.parametrize("stage", ["recovery", "lock"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("held", [False, True])
async def test_wait_preserves_facts_and_drains_only_this_call(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, read, dispatches: list[str], deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, stage: str, cancel: bool, held: bool,
) -> None:
    rid, peer = [str((await bundle.create())["resource_id"]) for _ in range(2)]
    owner = bundle.owner
    assert owner is not None
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None
    recover = owner.recover
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = owner._resource_lock(rid)

    async def recovering(resource_ids=None) -> None:
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() == "admit-under-test" and stage == "recovery":
            entered.set()
            try:
                await release.wait()
            finally:
                drained.set()
        await recover(resource_ids)
        if task.get_name() == "admit-under-test" and stage == "lock":
            await lock.acquire()
            entered.set()

    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:admit", operation_id="admit") as operation:
        if held:
            await read(operation, rid)
        original = bundle.leases.leases_for_resource(rid)
        with monkeypatch.context() as patch:
            patch.setattr(owner, "recover", recovering)
            pending = asyncio.create_task(read(operation, rid), name="admit-under-test")
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(read(operation, peer), 1)
                before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                expected_calls = ([rid] if held else []) + [peer]
                assert dispatches == expected_calls
                if cancel:
                    pending.cancel("原工具调用取消")
                    with pytest.raises(asyncio.CancelledError, match="原工具调用取消"):
                        await pending
                else:
                    if deadlines:
                        deadline = deadlines[0].when()
                        assert deadline is not None
                        deadlines[0].reschedule(deadline - 31)
                    with pytest.raises(TimeoutError, match="operation 准入等待超过 30 秒") as caught:
                        await asyncio.wait_for(asyncio.shield(pending), 0.3)
                    assert len(deadlines) == 1 and deadlines[0].expired()
                    assert "operation_id=admit, holder_id=request:admit" in str(caught.value)
                    assert rid in str(caught.value) and str(bundle.root) not in str(caught.value)
                assert owner._store.path.read_bytes() == before
                assert ledger_path.read_bytes() == ledger
                assert dispatches == expected_calls
                cold = ExternalResourceLeaseLedger(state_path=ledger_path)
                assert cold.leases_for_resource(rid) == original
                if stage == "lock":
                    assert lock.locked() and not lock._waiters
                    lock.release()
                else:
                    assert drained.is_set()
                release.set()
                await asyncio.sleep(0)
                assert owner._store.path.read_bytes() == before
                assert ledger_path.read_bytes() == ledger
                assert dispatches == expected_calls
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                release.set()
                if lock.locked():
                    lock.release()
        await read(operation, rid)
        assert dispatches == [*expected_calls, rid]
        leases = bundle.leases.leases_for_resource(rid)
        assert len(leases) == 1 and leases[0].status == "active"
        if held:
            assert leases == original


async def test_recovery_and_lock_share_one_deadline(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, read, deadlines: list[asyncio.Timeout],
    advance_clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner = bundle.restart()
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None
    recover, sync = owner.recover, bundle.transport.sync_owner_revision
    synced, resume, locked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = owner._resource_lock(rid)

    async def synchronizing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        await sync(resource_id, revision, for_stop=for_stop)
        synced.set()
        await resume.wait()

    async def recovering(resource_ids=None) -> None:
        await recover(resource_ids)
        if resource_ids == (rid,):
            await lock.acquire()
            locked.set()

    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:stages", operation_id="stages") as operation:
        with monkeypatch.context() as patch:
            patch.setattr(bundle.transport, "sync_owner_revision", synchronizing)
            patch.setattr(owner, "recover", recovering)
            pending = asyncio.create_task(read(operation, rid), name="admit-under-test")
            try:
                await asyncio.wait_for(synced.wait(), 1)
                advance_clock(20)
                resume.set()
                await asyncio.wait_for(locked.wait(), 1)
                record = owner._store.get(rid)
                assert record is not None and record.owner_revision == 2
                assert record.reconciliation_status == "verified"
                before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                advance_clock(11)
                with pytest.raises(TimeoutError, match="operation 准入等待超过 30 秒"):
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert len(deadlines) == 1 and deadlines[0].expired()
                assert owner._store.path.read_bytes() == before
                assert ledger_path.read_bytes() == ledger
                assert not bundle.leases.leases_for_resource(rid)
                assert lock.locked() and not lock._waiters
            finally:
                resume.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if lock.locked():
                    lock.release()
        await read(operation, rid)
        record = owner._store.get(rid)
        assert record is not None and record.owner_revision == 2


@pytest.mark.parametrize("invalid", ["association", "closed"])
async def test_validation_failure_releases_lock_without_new_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, read, invalid: str,
) -> None:
    rid = str((await bundle.create(thread=CHILD_THREAD_A if invalid == "association" else None))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    lock = owner._resource_lock(rid)
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:invalid", operation_id="invalid") as operation:
        await lock.acquire()
        pending = asyncio.create_task(read(operation, rid))
        try:
            await asyncio.sleep(0)
            assert not pending.done()
            if invalid == "closed":
                await operation.close()
            lock.release()
            with pytest.raises(ValueError if invalid == "association" else RuntimeError,
                               match="精确 Thread 关联" if invalid == "association" else "operation 已关闭"):
                await asyncio.wait_for(pending, 1)
            assert not lock.locked() and not lock._waiters
            assert not bundle.leases.leases_for_resource(rid)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if lock.locked():
                lock.release()


async def test_underlying_timeout_keeps_its_identity(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, read, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner = bundle.owner
    assert owner is not None
    original = TimeoutError("原恢复错误")

    async def failing(_resource_ids=None) -> None:
        raise original

    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:original", operation_id="original") as operation:
        with monkeypatch.context() as patch:
            patch.setattr(owner, "recover", failing)
            with pytest.raises(TimeoutError) as caught:
                await read(operation, rid)
            assert caught.value is original
        assert not owner._resource_lock(rid).locked()
        assert not bundle.leases.leases_for_resource(rid)


async def test_body_outlives_admission_budget_and_keeps_original_lock(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, read, deadlines: list[asyncio.Timeout],
    advance_clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner = bundle.owner
    assert owner is not None
    entered, release = asyncio.Event(), asyncio.Event()
    method = "read_terminal" if isinstance(bundle, TerminalOwnerFixture) else "perform"
    dispatch = getattr(bundle.transport, method)

    async def waiting(*args, **kwargs):
        entered.set()
        await release.wait()
        return await dispatch(*args, **kwargs)

    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:body", operation_id="body") as operation:
        with monkeypatch.context() as patch:
            patch.setattr(bundle.transport, method, waiting)
            pending = asyncio.create_task(read(operation, rid), name="admit-under-test")
            try:
                await asyncio.wait_for(entered.wait(), 1)
                lease, = bundle.leases.leases_for_resource(rid)
                advance_clock(31)
                await asyncio.sleep(0)
                assert not pending.done() and owner._resource_lock(rid).locked()
                assert len(deadlines) == 1 and not deadlines[0].expired()
                assert bundle.leases.get_lease(lease.lease_id) == lease
                release.set()
                await asyncio.wait_for(pending, 1)
                assert not owner._resource_lock(rid).locked()
                record = owner._store.get(rid)
                assert record is not None and record.reconciliation_status == "verified"
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
