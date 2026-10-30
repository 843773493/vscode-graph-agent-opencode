"""删除接管未发布child时，同步读取、定点清理和中止均须结算原worker。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import app.core.thread_creation as creation
from app.agents.graph_binding import compute_capability_profile_hash
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.session_subtree_delete import (
    SessionSubtreeDeleteService,
    SubtreeDeleteResult,
    SubtreeDeleteWaitTimeout,
)
from app.core.thread_creation import ThreadCreationService
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
    WORKSPACE_ID,
    create_physical_session,
    date_bucket_dir,
)
from tests.unit.core.test_session_subtree_delete import (
    service as service,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import store as store  # noqa: PLC0414
from tests.unit.core.test_session_subtree_delete_deadline import (
    advance_delete_clock as advance_delete_clock,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import make_metadata


@dataclass
class CreationWait:
    step: NativeStep
    blockers: list[sqlite3.Connection] = field(default_factory=list)
    removals: list[Path] = field(default_factory=list)

    def release(self) -> None:
        self.step.release.set()
        for blocker in self.blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")


@pytest.fixture
def creation_wait(
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Path, ThreadCreationRecord, str], CreationWait]]:
    probes: list[CreationWait] = []

    def install(source: Path, record: ThreadCreationRecord, stage: str) -> CreationWait:
        probe = CreationWait(native_step)
        probes.append(probe)
        database = source / "session-control.sqlite"
        staging = source / record.staging_locator
        pending, get = SessionControlStore.list_pending_thread_creations, SessionControlStore.get_thread_creation_record
        abort = SessionControlStore.abort_thread_creation_record
        read, remove, barrier = Path.read_bytes, creation.shutil.rmtree, creation._fsync_directory

        def pause(current_stage: str) -> None:
            if stage == current_stage and not probe.step.calls:
                probe.step.block()

        def list_pending(control: SessionControlStore) -> tuple[ThreadCreationRecord, ...]:
            if control.database_path == database:
                pause("pending")
            return pending(control)

        def get_record(control: SessionControlStore, key: str) -> ThreadCreationRecord:
            if control.database_path == database and key == record.thread_creation_idempotency_key:
                pause("record")
            return get(control, key)

        def read_bytes(path: Path) -> bytes:
            if path == staging / "thread.json":
                pause("verify")
            return read(path)

        def rmtree(path: Path) -> None:
            if path == staging:
                probe.removals.append(path)
                pause("remove")
            remove(path)

        def fsync(path: Path) -> None:
            if path == staging.parent:
                pause("barrier")
            barrier(path)

        def abort_record(control: SessionControlStore, key: str, reason: str) -> ThreadCreationRecord:
            if stage == "abort" and control.database_path == database and key == record.thread_creation_idempotency_key and not probe.step.calls:
                blocker = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
                blocker.execute("BEGIN IMMEDIATE")
                probe.blockers.append(blocker)
                probe.step.calls.append(threading.get_ident())
                probe.step.entered.set()
            return abort(control, key, reason)

        monkeypatch.setattr(SessionControlStore, "list_pending_thread_creations", list_pending)
        monkeypatch.setattr(SessionControlStore, "get_thread_creation_record", get_record)
        monkeypatch.setattr(SessionControlStore, "abort_thread_creation_record", abort_record)
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        monkeypatch.setattr(creation.shutil, "rmtree", rmtree)
        monkeypatch.setattr(creation, "_fsync_directory", fsync)
        return probe

    yield install
    for probe in probes:
        probe.release()
        for blocker in probe.blockers:
            blocker.close()


@pytest.mark.parametrize("stage,wait_kind", [
    ("pending", "native"), ("record", "native"), ("verify", "native"),
    ("remove", "native"), ("barrier", "native"), ("abort", "native"),
    ("verify", "pool"),
])
@pytest.mark.parametrize("completion", ["done", "cancel", "deadline"])
async def test_delete_settles_original_child_creation_io_before_releasing_owner(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, monkeypatch: pytest.MonkeyPatch,
    creation_wait: Callable[[Path, ThreadCreationRecord, str], CreationWait],
    advance_delete_clock: Callable[[float], None], stage: str, wait_kind: str,
    completion: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    peer = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    manifest = (source / "session.json").read_bytes()
    child_key, delete_key = "pending-child-io", "delete-pending-child-io"
    interruption = RuntimeError("创建在原rename前中断")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise interruption

    with closing(SessionControlStore(source / "session-control.sqlite")) as control:
        child_service = ThreadCreationService(
            store=store, control_store=control, sessions_root=sessions_root,
            workspace_id=WORKSPACE_ID, compute_capability_profile_hash=compute_capability_profile_hash,
        )
        with monkeypatch.context() as fault:
            fault.setattr(ThreadCreationService, "_rename_staging_to_final", interrupt)
            with pytest.raises(RuntimeError) as caught:
                await child_service.create(
                    idempotency_key=child_key, session_id=node.node_id, thread_id=None,
                    delegation_id=None, initial_state="idle",
                    session_metadata=make_metadata(task_seed=None, task_reference=None),
                    artifact_manifest={"notes/plan.md": "原计划"},
                )
        assert caught.value is interruption
        record = control.get_thread_creation_record(child_key)
        assert record.state == "preparing" and not control.list_child_thread_rows()
    staging = source / record.staging_locator
    assert (staging / "thread.json").is_file()
    probe = creation_wait(source, record, stage)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous_executor = getattr(loop, "_default_executor", None)
    executor: ThreadPoolExecutor | None = None
    pool_step = NativeStep()
    pool_queued = asyncio.Event()
    queued: asyncio.Future[None] | None = None
    deleting: asyncio.Task[SubtreeDeleteResult] | None = None
    holder_waiter: asyncio.Task[None] | None = None
    holder_entered = asyncio.Event()
    holders = OperationHolderGate(sessions_root, namespace="thread_creation")
    get_record = SessionControlStore.get_thread_creation_record

    def occupy_pool() -> None:
        nonlocal queued
        if queued is None:
            queued = loop.run_in_executor(None, pool_step.block)
            pool_queued.set()

    def get(control: SessionControlStore, key: str) -> ThreadCreationRecord:
        result = get_record(control, key)
        if wait_kind == "pool" and key == child_key and queued is None:
            loop.call_soon_threadsafe(occupy_pool)
        return result

    async def same_holder() -> None:
        async with holders.lock_for(f"{node.node_id}/{child_key}"):
            holder_entered.set()

    monkeypatch.setattr(SessionControlStore, "get_thread_creation_record", get)
    try:
        if wait_kind == "pool":
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete-child")
            loop.set_default_executor(executor)
        deleting = asyncio.create_task(service.delete(idempotency_key=delete_key, root_node_id=node.node_id))
        if wait_kind == "native":
            await _entered(probe.step)
        else:
            await asyncio.wait_for(pool_queued.wait(), 2)
            await _entered(pool_step)
            assert executor is not None
            # 原记录读取先交付，再核实清理已排队；取消不能落在前一阶段。
            async with asyncio.timeout(2):
                while executor._work_queue.qsize() == 0:
                    await asyncio.sleep(0.001)
            assert not probe.step.calls
        journal = store.get_subtree_delete_record(delete_key)
        assert journal.state == "deleting" and journal.drained_session_ids == ()
        assert (source / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
        if completion == "cancel":
            deleting.cancel("取消原child清理等待")
        elif completion == "deadline":
            advance_delete_clock(31)
        if stage != "pending":
            holder_waiter = asyncio.create_task(same_holder())
        await _turns()
        assert not deleting.done() and service._key_lock(delete_key).locked()
        assert not holder_entered.is_set()
        async with service._session_gate.exclusive(peer.node_id):
            assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(delete_key) == journal
        if wait_kind == "pool":
            pool_step.release.set()
            await _entered(probe.step)
        probe.release()
        if completion == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原child清理等待"):
                await deleting
        elif completion == "deadline":
            with pytest.raises(SubtreeDeleteWaitTimeout, match="phase=drain"):
                await deleting
        else:
            assert (await deleting).record_state == "completed"
        if holder_waiter is not None:
            await holder_waiter
            assert holder_entered.is_set()
        assert len(probe.step.calls) == 1 and probe.step.calls[0] != loop_thread
        if completion != "done":
            assert store.get_subtree_delete_record(delete_key) == journal
            with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
                current = control.get_thread_creation_record(child_key)
                assert current.state == ("aborted" if stage == "abort" else "preparing")
                assert control.get_fence() == ("active", 1)
            assert staging.exists() == (stage in ("pending", "record"))
            assert (source / "session.json").read_bytes() == manifest
            assert (await service.delete(idempotency_key=delete_key, root_node_id=node.node_id)).record_state == "completed"
        target = sessions_root / ".deleting" / delete_key / node.node_id
        assert probe.removals == [staging]
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
            aborted = control.get_thread_creation_record(child_key)
            assert aborted.state == "aborted" and aborted.abort_reason == "session_deletion_pending"
            assert not control.list_child_thread_rows()
    finally:
        probe.release()
        pool_step.release.set()
        tasks: list[Awaitable[object]] = [task for task in (deleting, holder_waiter, queued) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        if executor is not None:
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)
