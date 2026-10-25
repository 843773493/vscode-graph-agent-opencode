"""资源管理命令的协作式总等待预算不改变原操作与终态归属。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
async def advance_clock(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    original = loop.time
    offset = 0

    def advance(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: original() + offset)
    return advance


@pytest.mark.parametrize("stage", ["recovery", "admission", "lock"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_command_wait_before_dispatch_preserves_original_facts(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    advance_clock, stage: str, cancel: bool,
) -> None:
    resource = await bundle.create()
    owner = bundle.owner
    rid = resource["resource_id"]
    original = owner._store.get(rid)
    owner_bytes = owner._store.path.read_bytes()
    ledger_bytes = bundle.leases._state_path.read_bytes()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_recover(*_args):
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def blocked_admission(*_args):
        entered.set()
        await release.wait()
        yield ThreadRef(SESSION_A, THREAD_A)

    if stage == "recovery":
        monkeypatch.setattr(owner, "recover", blocked_recover)
    elif stage == "admission":
        monkeypatch.setattr(owner, "admit_thread", blocked_admission)
    lock = owner._resource_lock(rid)
    if stage == "lock":
        recover = owner.recover

        async def hold_after_recovery(*args):
            await recover(*args)
            await lock.acquire()
            entered.set()

        monkeypatch.setattr(owner, "recover", hold_after_recovery)
    pending = asyncio.create_task(bundle.command(
        rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A),
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            pending.cancel("caller-cancelled")
            with pytest.raises(asyncio.CancelledError, match="caller-cancelled"):
                await pending
        else:
            advance_clock(31)
            with pytest.raises(TimeoutError, match="管理命令等待超过 30 秒") as caught:
                await asyncio.wait_for(asyncio.shield(pending), 1)
            assert str(rid) in str(caught.value) and str(bundle.root) not in str(caught.value)
        assert owner._store.get(rid) == original
        assert owner._store.path.read_bytes() == owner_bytes
        assert bundle.leases._state_path.read_bytes() == ledger_bytes
        assert not bundle.transport.deleted
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        release.set()
        if stage == "lock":
            lock.release()
    await asyncio.sleep(0)
    assert owner._store.get(rid) == original


async def test_cold_recovery_and_dispatched_command_share_one_deadline_and_keep_exact_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    advance_clock,
) -> None:
    rid = (await bundle.create())["resource_id"]
    owner = bundle.restart()
    original_sync = bundle.transport.sync_owner_revision
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision))
        await original_sync(resource_id, revision, for_stop=for_stop)
        if len(calls) == 1:
            advance_clock(20)
        else:
            entered.set()
            await release.wait()

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "sync_owner_revision", syncing)
        pending = asyncio.create_task(bundle.command(rid, action="promote", expected_revision=2))
        try:
            # 本场景会推进20秒，观察者预算不参与被测命令的30秒合同。
            await asyncio.wait_for(entered.wait(), 60)
            lease, = bundle.leases.leases_for_resource(rid)
            assert lease.status == "active"
            advance_clock(11)
            with pytest.raises(TimeoutError, match="管理命令等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 1)
            saved = owner._store.get(rid)
            assert saved.reconciliation_status == "unknown" and saved.owner_revision == 3
            assert saved.retention_scope == "thread"
            cold = type(bundle.leases)(state_path=bundle.leases._state_path)
            retained = cold.get_lease(lease.lease_id)
            assert retained.status == "reconcile_required"
            assert (retained.resource_id, retained.operation_id) == (rid, lease.operation_id)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
    assert calls == [(rid, 2), (rid, 3)]
    # 故障解除后新管理身份沿原停止链核实，不重放失败的promote。
    stopped = await owner.command(rid, action="stop", expected_revision=3,
                                  holder_id="request:reconcile", operation_id="reconcile")
    assert stopped["reconciliation_status"] == "verified"
    assert bundle.leases.get_lease(lease.lease_id).status == "settled"


async def test_transport_timeout_is_preserved_as_original_error(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = (await bundle.create())["resource_id"]
    failure = TimeoutError("原传输超时")

    async def failed_sync(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", failed_sync)
    with pytest.raises(TimeoutError) as caught:
        await bundle.command(rid, action="promote", expected_revision=1)
    assert caught.value is failure
    assert bundle.owner._store.get(rid).reconciliation_status == "unknown"
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.status == "reconcile_required"
