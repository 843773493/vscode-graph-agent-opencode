"""资源记录的原生写入不能阻塞事件循环，也不能脱离原锁与恢复版本。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_B, THREAD_B
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)

OwnerFixture = TerminalOwnerFixture | BrowserOwnerFixture
WriteBarrier = tuple[asyncio.Event, threading.Event, list[int], Callable[[int], None]]


@pytest.fixture(params=["terminal", "browser"])
def resource_owner(request: pytest.FixtureRequest, tmp_path: Path) -> OwnerFixture:
    return (build_terminal_owner_fixture(tmp_path) if request.param == "terminal"
            else build_browser_owner_fixture(tmp_path))


@pytest.fixture
async def write_barrier(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WriteBarrier]:
    entered, release = asyncio.Event(), threading.Event()
    writers: list[int] = []
    loop = asyncio.get_running_loop()
    assert resource_owner.owner is not None
    pending = resource_owner.owner._store.path.with_suffix(".tmp")
    replace = Path.replace
    blocked_write = 1

    def publishing(path: Path, target: Path) -> Path:
        if path == pending:
            writers.append(threading.get_ident())
            if len(writers) == blocked_write:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("测试原生发布屏障未释放")
        return replace(path, target)

    def install(ordinal: int) -> None:
        nonlocal blocked_write
        blocked_write = ordinal
        monkeypatch.setattr(Path, "replace", publishing)

    # 创建阶段尚不安装故障；各场景生成产品状态后才启用同一原生边界。
    try:
        yield entered, release, writers, install
    finally:
        release.set()


async def test_concurrent_owner_commands_preserve_both_records_and_keep_native_write_off_loop(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier,
) -> None:
    first, second = await resource_owner.create(), await resource_owner.create()
    owner = resource_owner.owner
    assert owner is not None
    entered, release, writers, install = write_barrier
    original = owner._store.path.read_bytes()
    install(1)
    commands = [asyncio.create_task(resource_owner.command(
        str(first["resource_id"]), action="promote", expected_revision=1,
    ))]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not commands[0].done()
        commands.append(asyncio.create_task(resource_owner.command(
            str(second["resource_id"]), action="promote", expected_revision=1,
        )))
        await asyncio.sleep(0)
        assert not commands[1].done() and owner._store.path.read_bytes() == original
        assert all(record.owner_revision == 1 for record in owner._store.all())
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*commands), 3)
        assert all(result["owner_revision"] == 2 and result["retention_scope"] == "workspace" for result in results)
        assert all(record.owner_revision == 2 and record.retention_scope == "workspace" for record in owner._store.all())
        cold = resource_owner.restart()
        assert cold._store.all() == owner._store.all()
        assert writers and all(writer != threading.get_ident() for writer in writers)
        assert resource_owner.transport.creates == 2
    finally:
        release.set()
        await asyncio.gather(*commands, return_exceptions=True)


async def test_cancelled_owner_command_retains_original_lock_until_native_publication_settles(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier,
) -> None:
    resource = await resource_owner.create()
    rid, owner = str(resource["resource_id"]), resource_owner.owner
    assert owner is not None
    entered, release, _writers, install = write_barrier
    install(1)
    command = asyncio.create_task(resource_owner.command(rid, action="promote", expected_revision=1))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        command.cancel("原请求取消")
        await asyncio.sleep(0)
        assert not command.done() and owner._resource_lock(rid).locked()
        assert resource_owner.transport.records[rid]["transport_owner_revision"] == 1
        command.cancel("再次取消不能放弃原写入")
        await asyncio.sleep(0)
        assert not command.done() and owner._resource_lock(rid).locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(command, 3)
        record = owner._store.get(rid)
        assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "unknown"
        lease, = owner._leases.leases_for_resource(rid)
        assert lease.status == "reconcile_required" and not owner._resource_lock(rid).locked()
        stopped = await owner.command(rid, action="stop", expected_revision=2,
                                      holder_id="new-stop-request", operation_id="new-stop-operation")
        assert stopped["reconciliation_status"] == "verified"
        settled = owner._leases.get_lease(lease.lease_id)
        assert settled is not None and settled.status == "settled"
        assert resource_owner.transport.creates == 1
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


async def test_initial_record_publication_keeps_original_lock_and_closes_unstarted_creation(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier,
) -> None:
    owner = resource_owner.owner
    assert owner is not None
    entered, release, _writers, install = write_barrier
    install(1)
    creating = asyncio.create_task(resource_owner.create())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not owner._store.ids() and len(owner._resource_locks) == 1
        rid, = owner._resource_locks
        assert owner._resource_locks[rid].locked() and resource_owner.transport.creates == 0
        creating.cancel("首条intent写入取消")
        await asyncio.sleep(0)
        assert not creating.done() and owner._resource_locks[rid].locked()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="首条intent写入取消"):
            await asyncio.wait_for(creating, 3)
        record = owner._store.get(rid)
        assert record is not None and record.status == "deleted" and record.owner_revision == 1
        assert record.reconciliation_status == "verified" and not record.thread_associations
        assert not owner._resource_lock(rid).locked() and resource_owner.transport.creates == 0
        external = owner._leases.get(rid)
        assert external is not None and external.status == "stopped"
        assert not owner._leases.leases_for_resource(rid) and not owner._leases._leases
        assert await owner.list_resources() == []
        detail = await owner.get_resource(rid)
        assert detail["status"] == "deleted" and detail["reconciliation_status"] == "verified"
        cold = resource_owner.restart()
        assert cold._store.get(rid) == record
        assert await cold.list_resources() == []
        detail = await cold.get_resource(rid)
        assert detail["status"] == "deleted" and detail["owner_revision"] == 1
        assert not cold._leases._leases and resource_owner.transport.creates == 0
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)


async def test_cancellation_while_native_write_is_queued_keeps_original_locks_until_settlement(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await resource_owner.create())["resource_id"])
    owner = resource_owner.owner
    assert owner is not None
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="resource-record-write")

    def occupy() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("测试线程池屏障未释放")

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        occupying = loop.run_in_executor(None, occupy)
        await asyncio.wait_for(entered.wait(), 2)
        before = owner._store.path.read_bytes()
        command = asyncio.create_task(resource_owner.command(rid, action="promote", expected_revision=1))
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert owner._store._write_lock.locked() and owner._resource_lock(rid).locked()
            command.cancel("排队时取消")
            await asyncio.sleep(0)
            command.cancel("重复取消不遗弃排队worker")
            await asyncio.sleep(0)
            assert not command.done() and owner._store.path.read_bytes() == before
            assert owner._store._write_lock.locked() and owner._resource_lock(rid).locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(command, 3)
            record = owner._store.get(rid)
            assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "unknown"
            lease, = owner._leases.leases_for_resource(rid)
            assert lease.status == "reconcile_required"
            assert not owner._store._write_lock.locked() and not owner._resource_lock(rid).locked()
            assert resource_owner.transport.records[rid]["transport_owner_revision"] == 1
        finally:
            release.set()
            await asyncio.gather(command, occupying, return_exceptions=True)
            executor.shutdown(wait=True)


@pytest.mark.parametrize("outcome", ["complete", "record_error", "ledger_error"])
async def test_unstarted_creation_cleanup_survives_repeated_cancellation_and_exposes_storage_failure(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    owner = resource_owner.owner
    assert owner is not None
    pending = owner._store.path.with_suffix(".tmp")
    loop = asyncio.get_running_loop()
    entered = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    replace = Path.replace
    writes = 0
    storage_error = OSError(28, "未派发 intent 的收尾写入失败")

    def publishing(path: Path, target: Path) -> Path:
        nonlocal writes
        if path == pending:
            index = writes
            writes += 1
            if index < 2:
                loop.call_soon_threadsafe(entered[index].set)
                if not release[index].wait(5):
                    raise TimeoutError("测试 intent 收尾写入屏障未释放")
            if index == 1 and outcome == "record_error":
                raise storage_error
        return replace(path, target)

    def fail_ledger(*_args: object, **_kwargs: object) -> None:
        raise storage_error

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", publishing)
        if outcome == "ledger_error":
            patch.setattr(owner._leases, "update_external_state", fail_ledger)
        creating = asyncio.create_task(resource_owner.create())
        try:
            await asyncio.wait_for(entered[0].wait(), 2)
            rid, = owner._resource_locks
            creating.cancel("首条 intent 取消")
            release[0].set()
            await asyncio.wait_for(entered[1].wait(), 2)
            assert owner._resource_lock(rid).locked() and not creating.done()
            record = owner._store.get(rid)
            assert record is not None and record.status == "creating"
            assert not owner._leases._leases and resource_owner.transport.creates == 0
            creating.cancel("收尾期间的再次取消")
            await asyncio.sleep(0)
            assert owner._resource_lock(rid).locked() and not creating.done()
            release[1].set()
            if outcome == "complete":
                with pytest.raises(asyncio.CancelledError, match="首条 intent 取消"):
                    await asyncio.wait_for(creating, 3)
                record = owner._store.get(rid)
                assert record is not None and record.status == "deleted"
                external = owner._leases.get(rid)
                assert external is not None and external.status == "stopped"
            else:
                with pytest.raises(OSError) as caught:
                    await asyncio.wait_for(creating, 3)
                assert caught.value.errno == 28
                assert isinstance(caught.value.__context__, asyncio.CancelledError)
                assert str(caught.value.__context__) == "首条 intent 取消"
                if outcome == "record_error":
                    assert caught.value.__cause__ is storage_error
                else:
                    assert caught.value is storage_error
                record = owner._store.get(rid)
                assert record is not None
                assert record.status == ("creating" if outcome == "record_error" else "deleted")
                external = owner._leases.get(rid)
                assert external is not None and external.status == "running"
            assert not owner._resource_lock(rid).locked()
            assert not owner._leases._leases and resource_owner.transport.creates == 0
        finally:
            for barrier in release:
                barrier.set()
            await asyncio.gather(creating, return_exceptions=True)
    cold = resource_owner.restart()
    rows = await cold.list_resources()
    if outcome == "record_error":
        row, = rows
        assert row["resource_id"] == rid and row["status"] == "creating"
        assert row["reconciliation_status"] == "unknown" and not row["available_actions"]
    else:
        assert rows == []
        detail = await cold.get_resource(rid)
        assert detail["status"] == "deleted" and detail["reconciliation_status"] == "verified"
    assert not cold._leases._leases and resource_owner.transport.creates == 0


async def test_cancelled_cold_preparation_finishes_single_frozen_version_before_peer_recovery(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await resource_owner.create()
    rid, owner = str(resource["resource_id"]), resource_owner.restart()
    entered, release, _writers, install = write_barrier
    revisions: list[int] = []
    sync = resource_owner.transport.sync_owner_revision

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        revisions.append(revision)
        await sync(resource_id, revision, for_stop=for_stop)

    monkeypatch.setattr(resource_owner.transport, "sync_owner_revision", syncing)
    install(1)
    recovering = [asyncio.create_task(owner.recover())]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        recovering[0].cancel("冷准备原请求取消")
        recovering.append(asyncio.create_task(owner.recover()))
        await asyncio.sleep(0)
        assert all(not task.done() for task in recovering)
        assert owner._recovery._pending is None and revisions == []
        release.set()
        with pytest.raises(asyncio.CancelledError, match="冷准备原请求取消"):
            await asyncio.wait_for(recovering[0], 3)
        await asyncio.wait_for(recovering[1], 3)
        record = owner._store.get(rid)
        assert record is not None and record.owner_revision == 2
        assert owner._recovery._pending == {} and revisions == [2]
        assert resource_owner.transport.creates == 1
    finally:
        release.set()
        await asyncio.gather(*recovering, return_exceptions=True)


@pytest.mark.parametrize("stage", ["dispatching", "committed"])
async def test_cancelled_fork_publication_preserves_exact_persistent_receipt(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier, stage: str,
) -> None:
    resource = await resource_owner.create(scope="workspace")
    rid, owner = str(resource["resource_id"]), resource_owner.owner
    assert owner is not None
    target = ThreadRef(SESSION_B, THREAD_B)
    entered, release, _writers, install = write_barrier
    install(1 if stage == "dispatching" else 3)
    command = asyncio.create_task(owner.attach_for_fork(
        rid, creation_key="native-write-fork", expected_revision=1, thread=target,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        command.cancel("fork原生写入期间取消")
        await asyncio.sleep(0)
        assert not command.done() and owner._resource_lock(rid).locked()
        lease, = owner._leases.leases_for_resource(rid)
        assert lease.status == "active"
        release.set()
        with pytest.raises(asyncio.CancelledError, match="fork原生写入期间取消"):
            await asyncio.wait_for(command, 3)
        record = owner._store.get(rid)
        assert record is not None
        receipt = owner.fork_attach_receipt(rid, creation_key="native-write-fork", expected_revision=1, thread=target)
        assert receipt is not None and receipt.state == stage and receipt.lease_id == lease.lease_id
        saved_lease = owner._leases.get_lease(lease.lease_id)
        assert saved_lease is not None
        if stage == "committed":
            assert record.reconciliation_status == "verified" and target in record.thread_associations
            assert record.owner_revision == receipt.committed_revision == 2
            assert saved_lease.status == "released"
        else:
            assert record.reconciliation_status == "unknown" and target not in record.thread_associations
            assert record.owner_revision == 1 and receipt.committed_revision is None
            assert saved_lease.status == "reconcile_required"
        snapshot, retry = await owner.attach_for_fork(
            rid, creation_key="native-write-fork", expected_revision=1, thread=target,
        )
        assert retry == receipt and snapshot["owner_revision"] == record.owner_revision
        assert resource_owner.transport.creates == 1
        assert resource_owner.restart()._store.get(rid) == record
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


async def test_cancel_during_failure_record_publication_keeps_original_batch_error(
    resource_owner: OwnerFixture, write_barrier: WriteBarrier, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [str((await resource_owner.create())["resource_id"]) for _ in range(2)]
    owner = resource_owner.restart()
    entered, release, _writers, install = write_barrier
    original = ConnectionError("原授权同步回执失败")
    blocked = asyncio.Event()
    sync = resource_owner.transport.sync_owner_revision

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        await sync(resource_id, revision, for_stop=for_stop)
        if resource_id == ids[0]:
            raise original
        blocked.set()
        await asyncio.Future()

    monkeypatch.setattr(resource_owner.transport, "sync_owner_revision", syncing)
    # 两次冷准备写入后，第三次写入是首项原错误的 unknown 记录。
    install(3)
    recovering = asyncio.create_task(owner.recover(ids))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(blocked.wait(), 2)
        recovering.cancel("原批次取消")
        await asyncio.sleep(0)
        assert not recovering.done() and owner._resource_lock(ids[0]).locked()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="原批次取消") as caught:
            await asyncio.wait_for(recovering, 3)
        assert isinstance(caught.value.__cause__, ExceptionGroup)
        assert caught.value.__cause__.exceptions == (original,)
        for rid in ids:
            record = owner._store.get(rid)
            assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "unknown"
            assert not owner._resource_lock(rid).locked()
        assert resource_owner.transport.creates == 2
    finally:
        release.set()
        await asyncio.gather(recovering, return_exceptions=True)
