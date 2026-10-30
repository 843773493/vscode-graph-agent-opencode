"""已删除节点的物理回收在 worker 结算前继续持有原 Session gate。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.core.session_subtree_delete as deletion
from app.core.session_catalog_store import SessionCatalogStore, SubtreeDeleteRecord
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from tests.unit.core.test_session_subtree_delete import create_physical_session
from tests.unit.core.test_session_subtree_delete import (
    service as service,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import store as store  # noqa: PLC0414


@dataclass
class NativeStep:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: list[int] = field(default_factory=list)

    def block(self) -> None:
        self.calls.append(threading.get_ident())
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("测试原生故障点未被事件循环释放")


@pytest.fixture
def native_step() -> Iterator[NativeStep]:
    step = NativeStep()
    try:
        yield step
    finally:
        step.release.set()


async def _turns() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _entered(step: NativeStep) -> None:
    async with asyncio.timeout(2):
        while not step.entered.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("stage", ["remove", "barrier"])
@pytest.mark.parametrize("wait_kind", ["native", "pool"])
@pytest.mark.parametrize("cancel", [False, True], ids=["wait", "cancel"])
async def test_gc_keeps_gate_and_loop_available_until_original_io_finishes(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    stage: str, wait_kind: str, cancel: bool,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    peer = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "gc-worker-ownership"
    await service.delete(idempotency_key=key, root_node_id=node.node_id)
    record = store.get_subtree_delete_record(key)
    target = sessions_root / ".deleting" / key / node.node_id
    now = datetime.fromisoformat(record.record_updated_at) + timedelta(days=2)
    original_remove, original_barrier = deletion.shutil.rmtree, deletion._fsync_directory
    loop_thread = threading.get_ident()

    def remove(path: Path) -> None:
        if path == target and stage == "remove":
            native_step.block()
        original_remove(path)

    def barrier(path: Path) -> None:
        if path == target.parent and stage == "barrier":
            native_step.block()
        original_barrier(path)

    monkeypatch.setattr(deletion.shutil, "rmtree", remove)
    monkeypatch.setattr(deletion, "_fsync_directory", barrier)
    loop = asyncio.get_running_loop()
    previous_executor = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="replay-gc")
    pool_step = NativeStep()
    queued = None
    collecting: asyncio.Task[datetime | None] | None = None
    concurrent: asyncio.Task[datetime | None] | None = None
    same_gate: asyncio.Task[None] | None = None
    gate_entered = asyncio.Event()

    async def take_same_gate() -> None:
        async with service._session_gate.exclusive(node.node_id):
            gate_entered.set()

    try:
        if wait_kind == "pool":
            loop.set_default_executor(executor)
            queued = loop.run_in_executor(None, pool_step.block)
            await _entered(pool_step)
        collecting = asyncio.create_task(service.collect_expired_operation_replays(now=now))
        if wait_kind == "native":
            await _entered(native_step)
        else:
            await _turns()
            assert not native_step.calls, "原回收应仍在唯一线程池中排队"
        assert not collecting.done()
        if cancel:
            collecting.cancel("取消原回收等待")
        same_gate = asyncio.create_task(take_same_gate())
        concurrent = asyncio.create_task(service.collect_expired_operation_replays(now=now))
        await _turns()
        assert not collecting.done() and not concurrent.done() and not gate_entered.is_set()
        async with service._session_gate.exclusive(peer.node_id):
            assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(key) == record
        native_step.release.set()
        pool_step.release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="取消原回收等待"):
                await collecting
        else:
            assert await collecting is None
        assert await concurrent is None
        await same_gate
        assert len(native_step.calls) == 1
        assert native_step.calls[0] != loop_thread
        assert not target.exists() and gate_entered.is_set()
        assert store.get_subtree_delete_record(key) == record
    finally:
        native_step.release.set()
        pool_step.release.set()
        pending: list[Awaitable[object]] = [task for task in (collecting, concurrent, same_gate, queued) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        if wait_kind == "pool":
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
        executor.shutdown(wait=True)


@pytest.mark.parametrize("offset", [timedelta(microseconds=-1), timedelta()])
async def test_gc_keeps_original_retention_deadline(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, offset: timedelta,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "gc-retention-boundary"
    await service.delete(idempotency_key=key, root_node_id=node.node_id)
    record: SubtreeDeleteRecord = store.get_subtree_delete_record(key)
    deadline = datetime.fromisoformat(record.record_updated_at) + timedelta(days=1, seconds=30)
    target = sessions_root / ".deleting" / key / node.node_id
    result = await service.collect_expired_operation_replays(now=deadline + offset)
    assert result == (deadline if offset else None)
    assert target.is_dir() == bool(offset)
    assert store.get_subtree_delete_record(key) == record


@pytest.mark.parametrize("stage", ["remove", "barrier"])
async def test_gc_propagates_original_io_failure_and_retries_frozen_locator(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "gc-original-failure"
    await service.delete(idempotency_key=key, root_node_id=node.node_id)
    record = store.get_subtree_delete_record(key)
    target = sessions_root / ".deleting" / key / node.node_id
    now = datetime.fromisoformat(record.record_updated_at) + timedelta(days=2)
    failure = PermissionError(13, "原生回收失败", str(target))
    original = deletion.shutil.rmtree if stage == "remove" else deletion._fsync_directory

    def fail(path: Path) -> None:
        if path == (target if stage == "remove" else target.parent):
            raise failure
        original(path)

    with monkeypatch.context() as patch:
        patch.setattr(deletion.shutil if stage == "remove" else deletion,
                      "rmtree" if stage == "remove" else "_fsync_directory", fail)
        with pytest.raises(PermissionError) as caught:
            await service.collect_expired_operation_replays(now=now)
        assert caught.value is failure
    assert target.exists() == (stage == "remove")
    assert store.get_subtree_delete_record(key) == record
    async with service._session_gate.exclusive(node.node_id):
        pass
    assert await service.collect_expired_operation_replays(now=now) is None
    assert not target.exists() and store.get_subtree_delete_record(key) == record
