"""外部派发前持久保护原lease，失败写盘不能让普通收尾假报已释放。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from app.services.business.terminal_resources.operations import TerminalOperations
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    LeasedOperation,
    ReleaseWrite,
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    leased_operation as leased_operation,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    release_write as release_write,  # noqa: PLC0414
)


async def call(operation: LeasedOperation) -> None:
    instance, rid, _ = operation
    if isinstance(instance, TerminalOperations):
        await instance.read_terminal(rid)
    else:
        await instance.perform(rid, "read", {})


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("storage", ["ledger", "owner"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_write_failure_keeps_original_dispatch_through_all_cleanup_and_recovery(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch, restart: bool, storage: str, cancel: bool,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    native = OSError(28, "回执丢失后的原持久存储失败", str(path))
    original = asyncio.CancelledError("外部调用原取消") if cancel else ConnectionError("外部回执丢失")
    dispatched = False
    calls = 0
    replace = Path.replace
    store_path = owner._store.path

    async def lost(*args, **kwargs):
        nonlocal dispatched, calls
        calls += 1
        protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert protected is not None
        assert (protected.resource_id, protected.operation_id, protected.turn_stream_id) == (
            rid, lease.operation_id, lease.turn_stream_id,
        )
        dispatched = True
        raise original

    def unavailable(source: Path, target: Path) -> Path:
        if dispatched and target == (path if storage == "ledger" else store_path):
            raise native
        return replace(source, target)

    method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, method, lost)
        fault.setattr(Path, "replace", unavailable)
        with pytest.raises(OSError) as caught:
            await call(leased_operation)
        assert caught.value.__cause__ is native and caught.value.__context__ is original
    protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert protected is not None
    await operation.close()
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    assert type(bundle.leases)(state_path=path).get_lease(lease.lease_id) == protected
    assert protected.status == ("dispatching" if storage == "ledger" else "reconcile_required")
    if restart:
        owner = bundle.restart()
    current = await owner.get_resource(rid)
    assert current["reconciliation_status"] != "verified" and current["available_actions"] == ["stop"]
    retained = bundle.leases.get_lease(lease.lease_id)
    assert retained is not None and retained.status in {"dispatching", "reconcile_required"}
    assert (retained.lease_id, retained.resource_id, retained.operation_id, retained.holder_thread_id) == (
        lease.lease_id, rid, lease.operation_id, lease.holder_thread_id,
    )
    stopped = await bundle.command(rid, action="stop", expected_revision=current["owner_revision"])
    assert stopped["reconciliation_status"] == "verified"
    assert stopped["status"] == ("terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed")
    settled = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled" and settled.operation_id == lease.operation_id
    assert calls == 1 and bundle.transport.creates == 1 and bundle.transport.deleted == []


async def test_pre_dispatch_storage_failure_does_not_enter_external_call_or_mark_unknown(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    before, records = path.read_bytes(), owner._store.path.read_bytes()
    replace = Path.replace
    calls = 0
    native = OSError(28, "派发准备尚未持久", str(path))

    async def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("准备未持久不得进入外部回调")

    def fail(source: Path, target: Path) -> Path:
        if target == path:
            raise native
        return replace(source, target)

    method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, method, unexpected)
        fault.setattr(Path, "replace", fail)
        with pytest.raises(OSError) as caught:
            await call(leased_operation)
        assert caught.value.__cause__ is native
    assert calls == 0 and path.read_bytes() == before and owner._store.path.read_bytes() == records
    assert bundle.leases.get_lease(lease.lease_id) == lease
    await operation.close()
    released = bundle.leases.get_lease(lease.lease_id)
    assert released is not None and released.status == "released"


async def test_formal_receipt_confirmation_failure_cannot_be_released_by_operation_or_turn(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    native = OSError(28, "正式回执占用发布失败", str(path))
    original = bundle.leases.finish_operation_dispatch

    def fail(lease_id: str) -> None:
        assert lease_id == lease.lease_id
        replace = Path.replace

        def unavailable(source: Path, target: Path) -> Path:
            if target == path:
                raise native
            return replace(source, target)

        with monkeypatch.context() as io_fault:
            io_fault.setattr(Path, "replace", unavailable)
            original(lease_id)

    with monkeypatch.context() as fault:
        fault.setattr(bundle.leases, "finish_operation_dispatch", fail)
        with pytest.raises(OSError) as caught:
            await call(leased_operation)
        assert caught.value.__cause__ is native
    record = owner._store.get(rid)
    assert record is not None and record.reconciliation_status == "verified"
    protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert protected is not None and protected.status == "dispatching"
    await operation.close()
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    assert bundle.leases.get_lease(lease.lease_id) == protected
    current = await owner.get_resource(rid)
    assert current["reconciliation_status"] != "verified"
    await bundle.command(rid, action="stop", expected_revision=current["owner_revision"])
    settled = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled"


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_cancelled_preparation_settles_original_write_without_dispatching(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    release_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool,
) -> None:
    operation, rid, lease = leased_operation
    owner, path, gate = bundle.owner, bundle.leases._state_path, release_write
    assert owner is not None and path is not None
    native = OSError(28, "未派发准备的撤回写盘失败", str(path))
    calls = 0

    async def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("准备期间取消不得进入外部回调")

    if rollback_fails:
        def fail(_lease_id: str) -> None:
            raise native
        monkeypatch.setattr(bundle.leases, "finish_operation_dispatch", fail)
    method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
    monkeypatch.setattr(bundle.transport, method, unexpected)
    pending = asyncio.create_task(call(leased_operation))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done() and owner._resource_lock(rid).locked()
        pending.cancel("原派发准备取消")
        await asyncio.sleep(0)
        pending.cancel("重复取消不能遗弃原准备")
        await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert caught.value.args == ("原派发准备取消",)
        if rollback_fails:
            assert caught.value.__cause__ is native
        assert calls == 0 and not owner._resource_lock(rid).locked()
        retained = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert retained is not None and retained.status == ("dispatching" if rollback_fails else "active")
        assert retained.operation_id == lease.operation_id
        await operation.close()
        released = bundle.leases.get_lease(lease.lease_id)
        assert released is not None and released.status == ("dispatching" if rollback_fails else "released")
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_inflight_turn_cleanup_retains_dispatch_until_formal_receipt_commits(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _rid, lease = leased_operation
    path = bundle.leases._state_path
    assert path is not None
    entered, release = asyncio.Event(), asyncio.Event()
    method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
    original = getattr(bundle.transport, method)

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(bundle.transport, method, paused)
    pending = asyncio.create_task(call(leased_operation))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not pending.done() and bundle.leases.release_turn_leases(lease.turn_stream_id) == []
        protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert protected is not None and protected.status == "dispatching"
        release.set()
        await pending
        confirmed = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert confirmed is not None and confirmed.status == "active"
        assert confirmed.lease_id == protected.lease_id and confirmed.operation_id == protected.operation_id
        await operation.close()
        released = bundle.leases.get_lease(lease.lease_id)
        assert released is not None and released.status == "released"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_close_during_dispatch_preparation_does_not_send_external_operation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    release_write: ReleaseWrite, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, rid, lease = leased_operation
    owner, gate = bundle.owner, release_write
    assert owner is not None
    calls = 0

    async def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("关闭期间不得继续派发")

    method = "read_terminal" if isinstance(operation, TerminalOperations) else "perform"
    monkeypatch.setattr(bundle.transport, method, unexpected)
    pending = asyncio.create_task(call(leased_operation))
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        closing = asyncio.create_task(operation.close())
        while not operation._closed:
            await asyncio.sleep(0)
        assert not closing.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(RuntimeError, match="operation 已关闭"):
            await pending
        await closing
        assert calls == 0
        retained = bundle.leases.get_lease(lease.lease_id)
        record = owner._store.get(rid)
        assert retained is not None and retained.status == "released"
        assert record is not None and record.reconciliation_status == "verified"
    finally:
        gate.release.set()
        waiting = [pending] if closing is None else [pending, closing]
        await asyncio.gather(*waiting, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "close"])
async def test_queued_preparation_does_not_write_or_dispatch_after_interruption(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    operation, rid, lease = leased_operation
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    loop, occupied, free = asyncio.get_running_loop(), asyncio.Event(), Event()
    before, records = path.read_bytes(), owner._store.path.read_bytes()
    marked: list[str] = []
    mark = bundle.leases.mark_operation_dispatched

    def tracked(lease_id: str) -> None:
        marked.append(lease_id)
        mark(lease_id)

    def occupying() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free.wait(5), "测试未释放派发准备前的原线程池占用"

    monkeypatch.setattr(bundle.leases, "mark_operation_dispatched", tracked)
    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        occupying_task = loop.run_in_executor(None, occupying)
        await asyncio.wait_for(occupied.wait(), 2)
        pending = asyncio.create_task(call(leased_operation))
        closing: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(2):
                while not owner._resource_lock(rid).locked():
                    await asyncio.sleep(0)
            if interruption == "cancel":
                pending.cancel("排队准备的原取消")
            else:
                closing = asyncio.create_task(operation.close())
                while not operation._closed:
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not pending.done() and path.read_bytes() == before and marked == []
            free.set()
            with pytest.raises(asyncio.CancelledError if interruption == "cancel" else RuntimeError) as caught:
                await pending
            if interruption == "cancel":
                assert caught.value.args == ("排队准备的原取消",)
                assert path.read_bytes() == before and bundle.leases.get_lease(lease.lease_id) == lease
            else:
                assert "operation 已关闭" in str(caught.value)
                assert closing is not None
                await closing
            assert marked == [] and owner._store.path.read_bytes() == records
            await operation.close()
            released = bundle.leases.get_lease(lease.lease_id)
            assert released is not None and released.status == "released"
        finally:
            free.set()
            waiting = [pending, occupying_task] if closing is None else [pending, occupying_task, closing]
            await asyncio.gather(*waiting, return_exceptions=True)
