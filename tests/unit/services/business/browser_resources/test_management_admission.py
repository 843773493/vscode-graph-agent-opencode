"""Browser 管理动作准入有界，执行期限及原占用继续由原动作管理。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.services.business.browser_resources import owner as owner_module
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)


@pytest.fixture
def bundle(tmp_path: Path) -> BrowserOwnerFixture:
    return build_browser_owner_fixture(tmp_path)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "management-under-test":
            captured.append(context)
        return context

    monkeypatch.setattr(owner_module, "timeout", capture, raising=False)
    return captured


@pytest.mark.parametrize(("stage", "with_thread"), [
    ("recovery", True), ("recovery", False), ("admission", True),
    ("lock", True), ("lock", False),
])
@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_drains_only_this_admission_without_dispatching_late(
    bundle: BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, stage: str, with_thread: bool, cancel: bool,
) -> None:
    rid, peer = [str((await bundle.create("workspace"))["resource_id"]) for _ in range(2)]
    owner = bundle.owner
    assert owner is not None
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recover, admit = owner.recover, owner.admit_thread
    lock = owner._resource_lock(rid)
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None

    async def wait() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            drained.set()

    async def recovering(resource_ids=None) -> None:
        if resource_ids == (rid,) and stage == "recovery":
            await wait()
        await recover(resource_ids)
        if resource_ids == (rid,) and stage == "lock":
            await lock.acquire()
            entered.set()

    @asynccontextmanager
    async def admitting(session_id: str, thread_id: str | None) -> AsyncIterator[ThreadRef]:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "management-under-test":
            await wait()
        async with admit(session_id, thread_id) as ref:
            yield ref

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        if stage == "admission":
            patch.setattr(owner, "admit_thread", admitting)
        pending = asyncio.create_task(owner.perform(
            rid, action="navigate", payload={"url": "https://late.test"}, expected_revision=1,
            holder_id="request:management", operation_id="management",
            thread=ThreadRef(SESSION_A, THREAD_A) if with_thread else None,
        ), name="management-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.wait_for(owner.perform(
                peer, action="read", payload={}, expected_revision=1,
                holder_id="request:peer", operation_id="peer",
            ), 1)
            runtime = result["browser"]
            assert isinstance(runtime, dict) and runtime["resource_id"] == peer
            before = owner._store.path.read_bytes()
            ledger = ledger_path.read_bytes()
            if cancel:
                pending.cancel("原管理动作取消")
                with pytest.raises(asyncio.CancelledError, match="原管理动作取消"):
                    await pending
            else:
                if deadlines:
                    deadline = deadlines[0].when()
                    assert deadline is not None
                    deadlines[0].reschedule(deadline - 31)
                with pytest.raises(TimeoutError, match="Browser 动作准入等待超过 30 秒") as caught:
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert "operation_id=management" in str(caught.value) and rid in str(caught.value)
                assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.path.read_bytes() == before
            assert ledger_path.read_bytes() == ledger
            assert [call[0] for call in bundle.transport.calls] == [peer]
            assert not bundle.leases.leases_for_resource(rid)
            if stage == "lock":
                assert lock.locked() and not lock._waiters
                lock.release()
            else:
                assert drained.is_set()
            release.set()
            await asyncio.sleep(0)
            assert owner._store.path.read_bytes() == before
            assert ledger_path.read_bytes() == ledger
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if lock.locked():
                lock.release()
    await owner.perform(rid, action="read", payload={}, expected_revision=1,
                        holder_id="request:retry", operation_id="retry")
    assert [call[0] for call in bundle.transport.calls] == [peer, rid]


async def test_recovery_and_lock_consume_one_entry_budget(
    bundle: BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    advance_clock: Callable[[float], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.restart()
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None
    recover = owner.recover
    lock = owner._resource_lock(rid)
    entered = asyncio.Event()

    async def recovering(resource_ids=None) -> None:
        await recover(resource_ids)
        advance_clock(20)
        await lock.acquire()
        entered.set()

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        pending = asyncio.create_task(owner.perform(
            rid, action="read", payload={}, expected_revision=2,
            holder_id="request:stages", operation_id="stages",
        ), name="management-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            record = owner._store.get(rid)
            assert record is not None and record.owner_revision == 2
            before = owner._store.path.read_bytes()
            ledger = ledger_path.read_bytes()
            advance_clock(11)
            with pytest.raises(TimeoutError, match="Browser 动作准入等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 0.3)
            assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.path.read_bytes() == before
            assert ledger_path.read_bytes() == ledger
            assert not bundle.transport.calls and not bundle.leases.leases_for_resource(rid)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if lock.locked():
                lock.release()


async def test_admitted_run_keeps_its_own_deadline_lock_and_lease(
    bundle: BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    advance_clock: Callable[[float], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    entered, release = asyncio.Event(), asyncio.Event()
    perform = bundle.transport.perform

    async def running(*args, **kwargs):
        entered.set()
        await release.wait()
        return await perform(*args, **kwargs)

    monkeypatch.setattr(bundle.transport, "perform", running)
    payload = {"code": "return 'late';", "timeoutMs": 60000}
    pending = asyncio.create_task(owner.perform(
        rid, action="run", payload=payload, expected_revision=1,
        thread=ThreadRef(SESSION_A, THREAD_A),
        holder_id="request:run", operation_id="run",
    ), name="management-under-test")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        lease, = bundle.leases.leases_for_resource(rid)
        advance_clock(31)
        await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        assert len(deadlines) == 1 and not deadlines[0].expired()
        assert bundle.leases.get_lease(lease.lease_id) == lease
        release.set()
        result = await asyncio.wait_for(pending, 1)
        runtime = result["browser"]
        assert isinstance(runtime, dict) and runtime["reconciliation_status"] == "verified"
        assert bundle.transport.calls == [(rid, "run", payload, "user:http")]
        assert not owner._resource_lock(rid).locked()
        released = bundle.leases.get_lease(lease.lease_id)
        assert released is not None and released.status == "released"
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("stage", ["recovery", "body"])
async def test_underlying_timeout_preserves_its_original_identity(
    bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    failure = TimeoutError("原传输期限")

    async def failed(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(owner if stage == "recovery" else bundle.transport,
                        "recover" if stage == "recovery" else "perform", failed)
    with pytest.raises(TimeoutError) as caught:
        await owner.perform(rid, action="read", payload={}, expected_revision=1,
                            holder_id="request:original", operation_id="original")
    assert caught.value is failure
    leases = bundle.leases.leases_for_resource(rid)
    assert len(leases) == (1 if stage == "body" else 0)
    if leases:
        assert leases[0].status == "reconcile_required"
