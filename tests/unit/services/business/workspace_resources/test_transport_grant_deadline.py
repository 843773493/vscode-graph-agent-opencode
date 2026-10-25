"""传输授权共用一次协作式预算，取消不代表外部授权或资源已消失。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        if asyncio.current_task().get_name() == "grant-under-test":
            captured.append(context)
        return context

    monkeypatch.setattr(owner_module, "timeout", capture)
    return captured


@pytest.fixture
def grant(bundle: TerminalOwnerFixture | BrowserOwnerFixture):
    async def call(resource_id: str, revision: int = 1):
        return await bundle.owner.transport_grant(
            resource_id, expected_revision=revision, thread=ThreadRef(SESSION_A, THREAD_A),
            holder_id="request:grant-under-test", operation_id="grant-under-test",
        )
    return call


@pytest.mark.parametrize("stage", ["initial-lock", "recovery", "admission", "issue-lock"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_predispatch_wait_keeps_original_facts_and_drains_only_its_waiter(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch,
    deadlines: list[asyncio.Timeout], stage: str, cancel: bool,
) -> None:
    rid, peer = str((await bundle.create())["resource_id"]), str((await bundle.create())["resource_id"])
    owner = bundle.owner
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recover, admission = owner.recover, owner.admit_thread
    lock = owner._resource_lock(rid)
    issued: list[str] = []
    issuing = bundle.transport.issue_transport_grant

    async def wait() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            drained.set()

    async def recovering(resource_ids=None) -> None:
        if asyncio.current_task().get_name() == "grant-under-test" and stage == "recovery":
            await wait()
            return
        await recover(resource_ids)
        if asyncio.current_task().get_name() == "grant-under-test" and stage == "issue-lock":
            await lock.acquire()
            entered.set()

    @asynccontextmanager
    async def admitting(*args):
        if asyncio.current_task().get_name() == "grant-under-test" and stage == "admission":
            await wait()
        async with admission(*args) as ref:
            yield ref

    async def issue(resource_id: str, **kwargs):
        issued.append(resource_id)
        return await issuing(resource_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        patch.setattr(owner, "admit_thread", admitting)
        patch.setattr(bundle.transport, "issue_transport_grant", issue)
        if stage == "initial-lock":
            await lock.acquire()
        pending = asyncio.create_task(grant(rid), name="grant-under-test")
        try:
            if stage == "initial-lock":
                await asyncio.sleep(0)
                assert not pending.done()
            else:
                await asyncio.wait_for(entered.wait(), 1)
            assert (await asyncio.wait_for(grant(peer), 1))["resource"]["resource_id"] == peer
            before, ledger = owner._store.path.read_bytes(), bundle.leases._state_path.read_bytes()
            if cancel:
                pending.cancel("原授权调用取消")
                with pytest.raises(asyncio.CancelledError, match="原授权调用取消"):
                    await pending
            else:
                if deadlines:
                    deadlines[0].reschedule(deadlines[0].when() - 31)
                with pytest.raises(TimeoutError, match="传输授权等待超过 30 秒"):
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.path.read_bytes() == before
            assert bundle.leases._state_path.read_bytes() == ledger
            assert issued == [peer] and not bundle.leases.leases_for_resource(rid)
            if stage in {"initial-lock", "issue-lock"}:
                assert lock.locked()
                lock.release()
            else:
                assert drained.is_set()
            release.set()
            await asyncio.sleep(0)
            assert issued == [peer]
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
            if lock.locked():
                lock.release()
    assert (await grant(rid))["resource"]["reconciliation_status"] == "verified"


@pytest.mark.parametrize("stage", ["snapshot", "issue"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_dispatched_wait_keeps_exact_unknown_lease_until_owner_proves_stop(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch,
    deadlines: list[asyncio.Timeout], stage: str, cancel: bool,
) -> None:
    rid, peer = str((await bundle.create())["resource_id"]), str((await bundle.create())["resource_id"])
    owner = bundle.owner
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    method = "get_resource" if stage == "snapshot" else "issue_transport_grant"
    original = getattr(bundle.transport, method)
    calls: list[str] = []

    async def receiving(resource_id: str, **kwargs):
        result = await original(resource_id, **kwargs)
        if asyncio.current_task().get_name() == "grant-under-test":
            calls.append(resource_id)
            entered.set()
            try:
                await release.wait()
            finally:
                drained.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, method, receiving)
        pending = asyncio.create_task(grant(rid), name="grant-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            original_lease, = bundle.leases.leases_for_resource(rid)
            assert original_lease.status == "active"
            assert (await asyncio.wait_for(grant(peer), 1))["resource"]["resource_id"] == peer
            if cancel:
                pending.cancel("原授权调用取消")
                with pytest.raises(asyncio.CancelledError, match="原授权调用取消"):
                    await pending
            else:
                if deadlines:
                    deadlines[0].reschedule(deadlines[0].when() - 31)
                with pytest.raises(TimeoutError, match="传输授权等待超过 30 秒"):
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert len(deadlines) == 1 and deadlines[0].expired()
            assert drained.is_set()
            assert owner._store.get(rid).reconciliation_status == "unknown"
            cold = ExternalResourceLeaseLedger(state_path=bundle.leases._state_path)
            pending_lease = cold.get_lease(original_lease.lease_id)
            assert pending_lease.status == "reconcile_required"
            assert (pending_lease.resource_id, pending_lease.operation_id) == (rid, "grant-under-test")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
    assert (await owner.get_resource(rid))["reconciliation_status"] == "unknown"
    assert bundle.leases.get_lease(original_lease.lease_id).status == "reconcile_required"
    stopped = await bundle.command(rid, action="stop", expected_revision=1)
    assert stopped["reconciliation_status"] == "verified" and stopped["owner_revision"] == 2
    assert bundle.leases.get_lease(original_lease.lease_id).status == "settled"
    assert calls == [rid]


async def test_cold_recovery_and_issue_share_original_budget_without_reissuing_grant(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch,
    deadlines: list[asyncio.Timeout],
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner = bundle.restart()
    sync, issue = bundle.transport.sync_owner_revision, bundle.transport.issue_transport_grant
    entered, release = asyncio.Event(), asyncio.Event()
    revisions, issued = [], []

    async def syncing(resource_id: str, revision: int, *, for_stop: bool):
        await sync(resource_id, revision, for_stop=for_stop)
        revisions.append(revision)
        if deadlines:
            deadlines[0].reschedule(deadlines[0].when() - 20)

    async def issuing(resource_id: str, **kwargs):
        result = await issue(resource_id, **kwargs)
        issued.append(resource_id)
        entered.set()
        await release.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "sync_owner_revision", syncing)
        patch.setattr(bundle.transport, "issue_transport_grant", issuing)
        pending = asyncio.create_task(grant(rid, 2), name="grant-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert revisions == [2] and owner._store.get(rid).owner_revision == 2
            lease, = bundle.leases.leases_for_resource(rid)
            if deadlines:
                deadlines[0].reschedule(deadlines[0].when() - 11)
            with pytest.raises(TimeoutError, match="传输授权等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 0.3)
            assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.get(rid).owner_revision == 2
            assert owner._store.get(rid).reconciliation_status == "unknown"
            assert bundle.leases.get_lease(lease.lease_id).status == "reconcile_required"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
    assert issued == [rid]
    stopped = await bundle.command(rid, action="stop", expected_revision=2)
    assert stopped["owner_revision"] == 3 and bundle.leases.get_lease(lease.lease_id).status == "settled"


@pytest.mark.parametrize("stage", ["snapshot", "issue"])
async def test_transport_timeout_keeps_original_error_and_unknown_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    failure = TimeoutError("原授权传输超时")

    async def failed(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(bundle.transport, "get_resource" if stage == "snapshot" else "issue_transport_grant", failed)
    with pytest.raises(TimeoutError) as caught:
        await grant(rid)
    assert caught.value is failure
    assert bundle.owner._store.get(rid).reconciliation_status == "unknown"
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.status == "reconcile_required"
