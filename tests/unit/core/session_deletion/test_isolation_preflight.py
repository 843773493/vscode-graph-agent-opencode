"""隔离预检与真实SQLite写锁等待不阻塞事件循环。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

import app.core.session_subtree_delete as deletion
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_subtree_delete import (
    SessionSubtreeDeleteService,
    SubtreeDeleteResult,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
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


@pytest.mark.parametrize("stage,wait_kind", [
    ("source-stat", "native"), ("fence-write", "native"),
    ("recovered-target", "native"), ("source-stat", "pool"),
])
@pytest.mark.parametrize("completion", ["done", "cancel", "deadline"])
async def test_preflight_preserves_original_gate_until_native_access_settles(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    advance_delete_clock: Callable[[float], None], stage: str, wait_kind: str,
    completion: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    peer = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    manifest = (source / "session.json").read_bytes()
    key = "isolation-preflight-original"
    target = sessions_root / ".deleting" / key / node.node_id
    recovered = stage == "recovered-target"
    physical = target if recovered else source
    original_barrier = deletion._fsync_directory
    if recovered:
        failure = PermissionError(13, "真实隔离后的原屏障故障")

        def fail_barrier(path: Path) -> None:
            if path == target.parent:
                raise failure
            original_barrier(path)

        with monkeypatch.context() as patch:
            patch.setattr(deletion, "_fsync_directory", fail_barrier)
            with pytest.raises(PermissionError) as caught:
                await service.delete(idempotency_key=key, root_node_id=node.node_id)
            assert caught.value.__cause__ is failure

    drained = asyncio.Event()

    async def drain(_session_id: str) -> None:
        drained.set()

    service.set_session_drain_callback(drain)
    original_is_dir, original_cas = Path.is_dir, SessionControlStore.cas_fence_transition
    blockers: list[sqlite3.Connection] = []
    cas_threads: list[int] = []

    def is_dir(path: Path) -> bool:
        armed = recovered or drained.is_set()
        if stage != "fence-write" and path == physical and armed and not native_step.calls:
            native_step.block()
        return original_is_dir(path)

    def cas(control: SessionControlStore, expected_generation: int, new_state: str) -> bool:
        if control.database_path == source / "session-control.sqlite":
            cas_threads.append(threading.get_ident())
            if stage == "fence-write" and not native_step.calls:
                blocker = sqlite3.connect(control.database_path, isolation_level=None, check_same_thread=False)
                blocker.execute("BEGIN IMMEDIATE")
                blockers.append(blocker)
                native_step.calls.append(threading.get_ident())
                native_step.entered.set()
        return original_cas(control, expected_generation, new_state)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(SessionControlStore, "cas_fence_transition", cas)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous_executor = getattr(loop, "_default_executor", None)
    executor: ThreadPoolExecutor | None = None
    pool_step = NativeStep()
    queued: asyncio.Future[None] | None = None
    deleting: asyncio.Task[SubtreeDeleteResult] | None = None
    gate_waiter: asyncio.Task[None] | None = None
    gate_entered = asyncio.Event()

    async def same_gate() -> None:
        async with service._session_gate.exclusive(node.node_id):
            gate_entered.set()

    try:
        if wait_kind == "pool":
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="isolation-preflight")
            loop.set_default_executor(executor)
            queued = loop.run_in_executor(None, pool_step.block)
            await _entered(pool_step)
        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        if wait_kind == "native":
            await _entered(native_step)
        else:
            await asyncio.wait_for(drained.wait(), 2)
            await _turns()
            assert not native_step.calls and not cas_threads
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert record.frozen_session_locators == {node.node_id: node.storage_relative_locator}
        with closing(SessionControlStore(physical / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == (("deleting", 2) if recovered else ("active", 1))
        assert (physical / "session.json").read_bytes() == manifest
        if completion == "cancel":
            deleting.cancel("取消原隔离预检")
        elif completion == "deadline":
            advance_delete_clock(31)
        gate_waiter = asyncio.create_task(same_gate())
        await _turns()
        assert not deleting.done() and not gate_entered.is_set() and service._key_lock(key).locked()
        async with service._session_gate.exclusive(peer.node_id):
            assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(key) == record
        if wait_kind == "pool":
            pool_step.release.set()
            await _entered(native_step)
        native_step.release.set()
        for blocker in blockers:
            blocker.execute("ROLLBACK")
        if completion == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原隔离预检"):
                await deleting
        elif completion == "deadline":
            with pytest.raises(deletion.SubtreeDeleteWaitTimeout, match="phase=drain"):
                await deleting
        else:
            assert (await deleting).record_state == "completed"
        await gate_waiter
        assert gate_entered.is_set() and len(native_step.calls) == 1
        assert native_step.calls[0] != loop_thread
        if completion != "done":
            assert store.get_subtree_delete_record(key) == record
            assert (physical / "session.json").read_bytes() == manifest
            assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("deleting", 2)
        completed = store.get_subtree_delete_record(key)
        assert completed.state == "completed" and completed.drained_session_ids == (node.node_id,)
        assert completed.frozen_session_locators == record.frozen_session_locators
        assert recovered or all(thread_id != loop_thread for thread_id in cas_threads)
    finally:
        native_step.release.set()
        pool_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
            blocker.close()
        pending: list[Awaitable[object]] = [task for task in (deleting, gate_waiter, queued) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        if executor is not None:
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)
