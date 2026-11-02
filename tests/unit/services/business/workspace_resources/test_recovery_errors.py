"""批量冷恢复被取消或到期时，保全此前真实失败及原资源占用。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import recovery
from app.services.business.workspace_resources.recovery import RecoveryVersion
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    factory = build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture
    return factory(tmp_path)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    contexts: list[asyncio.Timeout] = []
    original = asyncio.timeout

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = original(seconds)
        contexts.append(context)
        return context

    monkeypatch.setattr(recovery, "timeout", capture)
    return contexts


@pytest.mark.parametrize("registered", [False, True], ids=["empty-owner", "unmatched-session"])
async def test_empty_listing_preserves_cold_check_without_repeating_transport(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, registered: bool,
) -> None:
    resource = await bundle.create("workspace") if registered else None
    owner = bundle.restart()
    listing = bundle.transport.list_resources
    calls = []

    async def observed() -> list[dict[str, object]]:
        calls.append(True)
        return await listing()

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "list_resources", observed)
        for _ in range(4):
            assert await owner.list_resources(session_id="ses_without_resources" if registered else None) == []
        # 首次冷恢复仍验证远端身份并同步原资源，后续空结果由原owner确定。
        assert len(calls) == 1
        if resource is not None:
            current, = await owner.list_resources()
            assert current["resource_id"] == resource["resource_id"]
            assert current["owner_revision"] == resource["owner_revision"] + 1
            assert current["reconciliation_status"] == "verified"
            assert len(calls) == 2


async def test_empty_owner_does_not_mask_initial_transport_failure(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.restart()
    failure = ConnectionError("首次资源清单未取得回执")

    async def failed() -> list[dict[str, object]]:
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "list_resources", failed)
        with pytest.raises(ConnectionError) as caught:
            await owner.list_resources()
        assert caught.value is failure and owner._recovery._pending is None
    assert await owner.list_resources() == []


@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_interrupted_recovery_keeps_completed_errors_and_original_pending_versions(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    ids = [str((await bundle.create("workspace"))["resource_id"]) for _ in range(4)]
    first, second, healthy, blocked = ids
    owner = bundle.owner
    assert owner is not None

    async def lost_receipt(*_arguments: object, **_options: object) -> dict[str, object]:
        raise ConnectionError("原工具请求未取得回执")

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "read_terminal" if isinstance(bundle, TerminalOwnerFixture) else "perform", lost_receipt)
        async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                                   holder_id="request:recovery-errors", operation_id="recovery-errors") as operation:
            for rid in (first, blocked):
                with pytest.raises(ConnectionError):
                    if isinstance(bundle, TerminalOwnerFixture):
                        await cast(TerminalOperations, operation).read_terminal(rid)
                    else:
                        await cast(BrowserOperations, operation).perform(rid, "read")
    leases = {rid: bundle.leases.leases_for_resource(rid)[0].lease_id for rid in (first, blocked)}
    owner = bundle.restart()
    ledger_ids = set(bundle.leases._leases)
    original_sync = bundle.transport.sync_owner_revision
    first_failed, second_failed, healthy_done, entered = (asyncio.Event() for _ in range(4))
    failures = [TimeoutError("外部同步自己的超时"), ConnectionError("另一资源同步失败")]
    calls: list[tuple[str, int, bool]] = []

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision, for_stop))
        await original_sync(resource_id, revision, for_stop=for_stop)
        if resource_id == first:
            # 反序完成仍须按原选择顺序归属，不能按回执先后拼接错误。
            await second_failed.wait()
            first_failed.set()
            raise failures[0]
        if resource_id == second:
            second_failed.set()
            raise failures[1]
        if resource_id == blocked:
            entered.set()
            await asyncio.Future()
        healthy_done.set()

    deadlines.clear()
    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "sync_owner_revision", syncing)
        waiting = asyncio.create_task(owner.recover(ids))
        try:
            for barrier in (first_failed, healthy_done, entered):
                await asyncio.wait_for(barrier.wait(), 2)
            assert len(deadlines) == 1 and not waiting.done()
            if ending == "deadline":
                deadlines[0].reschedule(asyncio.get_running_loop().time())
                with pytest.raises(ExceptionGroup) as caught:
                    await asyncio.wait_for(waiting, 2)
                assert caught.value.exceptions[:2] == tuple(failures)
                deadline = caught.value.exceptions[2]
                assert isinstance(deadline, TimeoutError) and "冷恢复等待超过 30 秒" in str(deadline)
            else:
                waiting.cancel("保留原取消归属")
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await waiting
                assert cancelled.value.args == ("保留原取消归属",)
                cause = cancelled.value.__cause__
                assert isinstance(cause, ExceptionGroup) and cause.exceptions == tuple(failures)
            assert owner._recovery._pending == {
                first: RecoveryVersion(2, management_only=True),
                second: RecoveryVersion(2, management_only=True),
                blocked: RecoveryVersion(2),
            }
            assert set(bundle.leases._leases) == ledger_ids
            for rid, lease_id in leases.items():
                lease, = bundle.leases.leases_for_resource(rid)
                assert lease.lease_id == lease_id and lease.status == "reconcile_required"
            for rid in (first, second, blocked):
                record = owner._store.get(rid)
                assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "unknown"
            assert all(not owner._resource_lock(rid).locked() for rid in ids)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    before = list(calls)

    async def recovered_sync(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision, for_stop))
        await original_sync(resource_id, revision, for_stop=for_stop)

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "sync_owner_revision", recovered_sync)
        await owner.recover(ids)
    # 普通失败的版本仍只允许管理重试；取消的一项沿同一版本恢复，不重放旧工具。
    assert calls == before + [(blocked, 2, False)]
    assert owner._recovery._pending == {
        first: RecoveryVersion(2, management_only=True), second: RecoveryVersion(2, management_only=True),
    }
    assert (await owner.get_resource(healthy))["reconciliation_status"] == "verified"
    assert bundle.transport.creates == 4 and not bundle.transport.deleted
    for rid, lease_id in leases.items():
        lease, = bundle.leases.leases_for_resource(rid)
        assert lease.lease_id == lease_id and lease.status == "reconcile_required"
