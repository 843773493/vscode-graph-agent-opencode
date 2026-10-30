"""删除与到期回收的 catalog 等待必须保留原 owner，允许事件循环继续调度。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_subtree_delete import (
    SessionSubtreeDeleteService,
    SubtreeDeleteWaitTimeout,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import create_physical_session
from tests.unit.core.test_session_subtree_delete import (
    service as service,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import store as store  # noqa: PLC0414

_STAGES = {
    "create": "create_or_get_subtree_delete_record",
    "mark": "mark_subtree_deleting",
    "progress": "record_drain_progress",
    "finish": "finish_subtree_delete",
}


@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
async def test_delete_settles_original_catalog_call_before_releasing_owner(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str, ending: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "catalog-original-owner"
    original = getattr(store, _STAGES[stage])
    loop_thread = threading.get_ident()

    def wait(*args, **kwargs):
        native_step.block()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, _STAGES[stage], wait)
    budget = asyncio.timeout(None)
    expected = {"create": "deleting", "mark": "deleting", "progress": "draining", "finish": "completed"}
    same_key_entered, topology_entered = asyncio.Event(), asyncio.Event()

    async def delete():
        async with budget:
            return await service.delete(idempotency_key=key, root_node_id=node.node_id)

    async def same_key() -> None:
        async with service._key_lock(key):
            same_key_entered.set()

    async def topology() -> None:
        async with service._gate.exclusive():
            topology_entered.set()

    deleting = asyncio.create_task(delete())
    waiters: list[asyncio.Task[None]] = []
    try:
        await _entered(native_step)
        waiters.append(asyncio.create_task(same_key()))
        if stage in ("create", "mark", "finish"):
            waiters.append(asyncio.create_task(topology()))
        if ending == "cancel":
            deleting.cancel("取消原 catalog 等待")
        elif ending == "deadline":
            budget.reschedule(asyncio.get_running_loop().time())
        await _turns()
        assert not deleting.done() and not same_key_entered.is_set() and not topology_entered.is_set()
        native_step.release.set()
        if ending == "complete":
            assert (await deleting).record_state == "completed"
        elif ending == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原 catalog 等待"):
                await deleting
        else:
            with pytest.raises(TimeoutError) as caught:
                await deleting
            assert not isinstance(caught.value, SubtreeDeleteWaitTimeout), "外层期限不得改归删除预算"
        await asyncio.gather(*waiters)
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        record = store.get_subtree_delete_record(key)
        assert record.state == ("completed" if ending == "complete" else expected[stage])
        if ending != "complete":
            assert record.drained_session_ids == ((node.node_id,) if stage in ("progress", "finish") else ())
        monkeypatch.setattr(store, _STAGES[stage], original)
        assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
    finally:
        native_step.release.set()
        await asyncio.gather(deleting, *waiters, return_exceptions=True)


@pytest.mark.parametrize("pool_wait", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_gc_catalog_snapshot_settles_before_cancellation_without_starting_removal(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, pool_wait: bool, cancel: bool,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "gc-catalog-snapshot"
    await service.delete(idempotency_key=key, root_node_id=node.node_id)
    record = store.get_subtree_delete_record(key)
    target = sessions_root / ".deleting" / key / node.node_id
    now = datetime.fromisoformat(record.record_updated_at) + timedelta(days=2)
    original = store.list_completed_subtree_deletes
    loop_thread = threading.get_ident()

    def list_records(workspace_id):
        native_step.block()
        return original(workspace_id)

    monkeypatch.setattr(store, "list_completed_subtree_deletes", list_records)
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=1)
    pool_step = NativeStep()
    tasks: list[Awaitable[object]] = []
    try:
        if pool_wait:
            loop.set_default_executor(executor)
            tasks.append(loop.run_in_executor(None, pool_step.block))
            await _entered(pool_step)
        collecting = asyncio.create_task(service.collect_expired_operation_replays(now=now))
        tasks.append(collecting)
        if pool_wait:
            await _turns()
            assert not native_step.calls
        else:
            await _entered(native_step)
        if cancel:
            collecting.cancel("取消原清单等待")
        await _turns()
        assert not collecting.done() and target.is_dir()
        async with service._session_gate.exclusive(node.node_id):
            assert store.get_subtree_delete_record(key) == record
        native_step.release.set()
        pool_step.release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="取消原清单等待"):
                await collecting
            assert target.is_dir(), "取消清单读取不得继续物理回收"
        else:
            assert await collecting is None and not target.exists()
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        assert store.get_subtree_delete_record(key) == record
        monkeypatch.setattr(store, "list_completed_subtree_deletes", original)
        assert await service.collect_expired_operation_replays(now=now) is None and not target.exists()
    finally:
        native_step.release.set()
        pool_step.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pool_wait:
            if previous is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous)
        executor.shutdown(wait=True)


@pytest.mark.parametrize("stage", _STAGES)
async def test_catalog_failure_keeps_original_exception_and_same_journal_retry(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore, sessions_root: Path,
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "catalog-original-error"
    original = getattr(store, _STAGES[stage])
    failure = sqlite3.OperationalError("原 catalog 写入失败")

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(store, _STAGES[stage], fail)
        with pytest.raises(sqlite3.OperationalError) as caught:
            await service.delete(idempotency_key=key, root_node_id=node.node_id)
    assert caught.value is failure
    assert getattr(store, _STAGES[stage]) == original
    assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"


async def test_real_sqlite_write_wait_allows_cancellation_then_settles_committed_mark(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "catalog-sqlite-writer"
    original = store.mark_subtree_deleting
    blockers: list[sqlite3.Connection] = []

    def mark(*args, **kwargs):
        blocker = sqlite3.connect(store.database_path, isolation_level=None, check_same_thread=False)
        blockers.append(blocker)
        blocker.execute("BEGIN IMMEDIATE")
        native_step.calls.append(threading.get_ident())
        native_step.entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "mark_subtree_deleting", mark)
    deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
    try:
        await _entered(native_step)
        deleting.cancel("取消真实 SQLite 写入等待")
        await _turns()
        assert not deleting.done()
        for blocker in blockers:
            blocker.rollback()
        with pytest.raises(asyncio.CancelledError, match="取消真实 SQLite 写入等待"):
            await deleting
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and not record.drained_session_ids
        assert store.get_node(node.node_id).state == "deleting"
        monkeypatch.setattr(store, "mark_subtree_deleting", original)
        assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
    finally:
        for blocker in blockers:
            blocker.rollback()
            blocker.close()
        await asyncio.gather(deleting, return_exceptions=True)
