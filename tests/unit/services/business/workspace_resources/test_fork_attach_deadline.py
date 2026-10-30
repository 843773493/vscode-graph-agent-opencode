"""Fork 资源关联共用一次等待预算，取消不改写原发布和回执事实。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_B, THREAD_B
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() == "fork-attach-under-test":
            captured.append(context)
        return context

    monkeypatch.setattr(owner_module, "timeout", capture)
    return captured


@pytest.mark.parametrize("stage", ["recovery", "lock", "dispatched", "committed"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_fork_wait_keeps_exact_receipt_and_does_not_replay_after_release(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    deadlines: list[asyncio.Timeout], monkeypatch: pytest.MonkeyPatch, stage: str, cancel: bool,
) -> None:
    rid, peer = [str((await bundle.create(scope="workspace"))["resource_id"]) for _ in range(2)]
    owner = bundle.owner
    assert owner is not None
    target = ThreadRef(SESSION_B, THREAD_B)
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recover, observe = owner.recover, owner_module.observe_phase
    lock = owner._resource_lock(rid)

    async def pause() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            drained.set()

    async def recovering(resource_ids=None) -> None:
        if resource_ids == (rid,) and stage == "recovery":
            await pause()
        await recover(resource_ids)
        if resource_ids == (rid,) and stage == "lock":
            await lock.acquire()
            entered.set()

    async def observing(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
        if operation_id == "fork-deadline" and phase == f"fork.live.{stage}.{owner.kind}":
            await pause()
        await observe(operation_id, phase, details=details)

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        patch.setattr(owner_module, "observe_phase", observing)
        pending = asyncio.create_task(owner.attach_for_fork(
            rid, creation_key="fork-deadline", expected_revision=1, thread=target,
        ), name="fork-attach-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (await asyncio.wait_for(owner.get_resource(peer), 1))["reconciliation_status"] == "verified"
            before = owner._store.path.read_bytes()
            if cancel:
                pending.cancel("原fork调用取消")
                with pytest.raises(asyncio.CancelledError, match="原fork调用取消"):
                    await pending
            else:
                if deadlines:
                    deadline = deadlines[0].when()
                    assert deadline is not None
                    deadlines[0].reschedule(deadline - 31)
                with pytest.raises(TimeoutError, match="fork 关联等待超过 30 秒") as caught:
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert len(deadlines) == 1 and deadlines[0].expired()
                assert rid in str(caught.value) and "fork-deadline" in str(caught.value)
                assert str(bundle.root) not in str(caught.value)
            record = owner._store.get(rid)
            assert record is not None
            receipt = owner.fork_attach_receipt(rid, creation_key="fork-deadline", expected_revision=1, thread=target)
            leases = bundle.leases.leases_for_resource(rid)
            if stage in {"recovery", "lock"}:
                assert owner._store.path.read_bytes() == before
                assert receipt is None and leases == [] and target not in record.thread_associations
            elif stage == "dispatched":
                assert receipt is not None and receipt.state == "dispatching"
                assert record.reconciliation_status == "unknown" and target not in record.thread_associations
                assert len(leases) == 1 and leases[0].lease_id == receipt.lease_id
                assert leases[0].status == "reconcile_required"
            else:
                assert receipt is not None and receipt.state == "committed" and receipt.committed_revision == 2
                assert record.reconciliation_status == "verified" and target in record.thread_associations and leases == []
            if stage == "lock":
                assert lock.locked() and not lock._waiters
                lock.release()
            else:
                assert drained.is_set()
            frozen = owner._store.path.read_bytes()
            release.set()
            await asyncio.sleep(0)
            assert owner._store.path.read_bytes() == frozen
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if stage == "lock" and lock.locked():
                lock.release()
    current, replayed = await owner.attach_for_fork(
        rid, creation_key="fork-deadline", expected_revision=1, thread=target,
    )
    if stage == "dispatched":
        assert replayed == receipt and current["reconciliation_status"] == "unknown"
        assert owner._store.get(rid) == record
    else:
        assert replayed.state == "committed" and current["owner_revision"] == 2
        current_record = owner._store.get(rid)
        assert current_record is not None and current_record.thread_associations.count(target) == 1
    assert bundle.transport.creates == 2


async def test_recovery_and_resource_lock_share_one_fork_budget(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    advance_clock: Callable[[float], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    recover = owner.recover
    first, resume, locked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = owner._resource_lock(rid)

    async def recovering(resource_ids=None) -> None:
        await recover(resource_ids)
        first.set()
        await resume.wait()
        await lock.acquire()
        locked.set()

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        pending = asyncio.create_task(owner.attach_for_fork(
            rid, creation_key="fork-budget-stages", expected_revision=1, thread=ThreadRef(SESSION_B, THREAD_B),
        ), name="fork-attach-under-test")
        try:
            await asyncio.wait_for(first.wait(), 1)
            advance_clock(20)
            resume.set()
            await asyncio.wait_for(locked.wait(), 1)
            assert not pending.done()
            advance_clock(11)
            with pytest.raises(TimeoutError, match="fork 关联等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 0.3)
            assert len(deadlines) == 1 and deadlines[0].expired()
            record = owner._store.get(rid)
            assert record is not None and not record.fork_attachments and not bundle.leases.leases_for_resource(rid)
            assert lock.locked() and not lock._waiters
        finally:
            resume.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if lock.locked():
                lock.release()


async def test_original_recovery_timeout_keeps_its_identity(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    original = TimeoutError("原恢复调用自己的错误")

    async def failing(resource_ids=None) -> None:
        raise original

    monkeypatch.setattr(owner, "recover", failing)
    with pytest.raises(TimeoutError) as caught:
        await owner.attach_for_fork(rid, creation_key="fork-original-timeout", expected_revision=1,
                                    thread=ThreadRef(SESSION_B, THREAD_B))
    record = owner._store.get(rid)
    assert caught.value is original and record is not None and not record.fork_attachments
