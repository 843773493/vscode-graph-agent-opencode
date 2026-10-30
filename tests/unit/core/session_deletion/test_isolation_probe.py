"""删除恢复的前序目标探测仅持同key锁，原生等待不阻塞事件循环。"""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("stage", ["missing-exists", "missing-symlink", "recovered-exists"])
@pytest.mark.parametrize("wait_kind", ["native", "pool"])
@pytest.mark.parametrize("completion", ["done", "cancel", "deadline"])
async def test_probe_settles_before_releasing_key_without_holding_session_gate(
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
    key = "isolation-target-probe"
    target = sessions_root / ".deleting" / key / node.node_id
    recovered = stage == "recovered-exists"
    physical = target if recovered else source
    management: list[tuple[str, str]] = []
    renames: list[tuple[Path, Path]] = []
    original_rename, original_barrier = deletion.os.rename, deletion._fsync_directory

    async def retention(session_id: str) -> None:
        management.append(("retention", session_id))

    async def drain(session_id: str) -> None:
        management.append(("drain", session_id))

    def rename(old: Path, new: Path) -> None:
        if old == source and new == target:
            renames.append((old, new))
        original_rename(old, new)

    service.set_fork_retention_release_callback(retention)
    service.set_session_drain_callback(drain)
    monkeypatch.setattr(deletion.os, "rename", rename)
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
    before_management, before_renames = list(management), list(renames)
    original_exists, original_symlink = Path.exists, Path.is_symlink
    reads: list[tuple[str, int]] = []

    def probe(path: Path, method: str, original: Callable[[Path], bool]) -> bool:
        if path == target:
            reads.append((method, threading.get_ident()))
            if method == ("is_symlink" if stage == "missing-symlink" else "exists") and not native_step.calls:
                native_step.block()
        return original(path)

    monkeypatch.setattr(Path, "exists", lambda path: probe(path, "exists", original_exists))
    monkeypatch.setattr(Path, "is_symlink", lambda path: probe(path, "is_symlink", original_symlink))
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous_executor = getattr(loop, "_default_executor", None)
    executor: ThreadPoolExecutor | None = None
    pool_step = NativeStep()
    queued: asyncio.Future[None] | None = None
    deleting: asyncio.Task[SubtreeDeleteResult] | None = None
    key_waiter: asyncio.Task[None] | None = None
    key_entered = asyncio.Event()
    probe_queued = asyncio.Event()
    original_drain = service._drain

    async def queue_probe(*args, **kwargs):
        nonlocal queued
        if wait_kind == "pool" and queued is None:
            queued = loop.run_in_executor(None, pool_step.block)
            await _entered(pool_step)
            probe_queued.set()
        return await original_drain(*args, **kwargs)

    monkeypatch.setattr(service, "_drain", queue_probe)

    async def same_key() -> None:
        async with service._key_lock(key):
            key_entered.set()

    try:
        if wait_kind == "pool":
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="isolation-target-probe")
            loop.set_default_executor(executor)
        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        if wait_kind == "native":
            await _entered(native_step)
        else:
            # catalog标记已提交，worker尚在唯一线程池排队。
            await probe_queued.wait()
            await _turns()
            assert not reads and not native_step.calls
        assert not deleting.done()
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert record.frozen_session_locators == {node.node_id: node.storage_relative_locator}
        assert management == before_management and renames == before_renames
        assert (physical / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(physical / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == (("deleting", 2) if recovered else ("active", 1))
        if completion == "cancel":
            deleting.cancel("取消原目标探测")
        elif completion == "deadline":
            advance_delete_clock(31)
        key_waiter = asyncio.create_task(same_key())
        await _turns()
        assert not deleting.done() and not key_entered.is_set() and service._key_lock(key).locked()
        # 前序探测尚未进入holder、Session gate或管理回调。
        async with asyncio.timeout(1):
            async with service._session_gate.exclusive(node.node_id):
                assert store.get_node(node.node_id).state == "deleting"
            async with service._session_gate.exclusive(peer.node_id):
                assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(key) == record
        if wait_kind == "pool":
            pool_step.release.set()
            await _entered(native_step)
        native_step.release.set()
        if completion == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原目标探测"):
                await deleting
        elif completion == "deadline":
            with pytest.raises(deletion.SubtreeDeleteWaitTimeout, match="phase=drain"):
                await deleting
        else:
            assert (await deleting).record_state == "completed"
        await key_waiter
        assert key_entered.is_set() and len(native_step.calls) == 1
        assert native_step.calls[0] != loop_thread
        if completion != "done":
            await _turns()
            assert store.get_subtree_delete_record(key) == record
            assert management == before_management and renames == before_renames
            assert (physical / "session.json").read_bytes() == manifest
            with closing(SessionControlStore(physical / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == (("deleting", 2) if recovered else ("active", 1))
            assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
        first_methods = [method for method, _ in reads[:2]]
        assert first_methods == (["exists", "exists"] if recovered else ["exists", "is_symlink"])
        assert all(thread_id != loop_thread for _, thread_id in reads)
        assert management == [("retention", node.node_id), ("drain", node.node_id)]
        assert renames == [(source, target)]
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = store.get_subtree_delete_record(key)
        assert completed.state == "completed" and completed.drained_session_ids == (node.node_id,)
        assert completed.frozen_session_locators == record.frozen_session_locators
    finally:
        native_step.release.set()
        pool_step.release.set()
        pending: list[Awaitable[object]] = [task for task in (deleting, key_waiter, queued) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        if executor is not None:
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)


@pytest.mark.parametrize("method", ["exists", "is_symlink"])
async def test_probe_preserves_original_error_and_retries_same_record(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    manifest = (source / "session.json").read_bytes()
    key = "isolation-probe-original-error"
    target = sessions_root / ".deleting" / key / node.node_id
    failure = PermissionError(13, "原生目标探测失败", str(target))
    original = getattr(Path, method)
    management: list[str] = []

    async def drain(session_id: str) -> None:
        management.append(session_id)

    def fail(path: Path) -> bool:
        if path == target:
            raise failure
        return original(path)

    service.set_session_drain_callback(drain)
    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(PermissionError) as caught:
            await service.delete(idempotency_key=key, root_node_id=node.node_id)
        assert caught.value is failure
    record = store.get_subtree_delete_record(key)
    assert record.state == "deleting" and record.drained_session_ids == () and not management
    assert (source / "session.json").read_bytes() == manifest and not target.exists()
    with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
        assert control.get_fence() == ("active", 1)
    assert not service._key_lock(key).locked()
    assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
    assert management == [node.node_id] and not source.exists()


async def test_dangling_target_is_rejected_before_management_and_not_overwritten(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    key = "isolation-probe-dangling-target"
    target = sessions_root / ".deleting" / key / node.node_id
    target.parent.mkdir(parents=True)
    destination = target.parent / "missing"
    target.symlink_to(destination, target_is_directory=True)
    management: list[str] = []

    async def drain(session_id: str) -> None:
        management.append(session_id)

    service.set_session_drain_callback(drain)
    with pytest.raises(RuntimeError, match="源日期桶目录与隔离目标同时存在"):
        await service.delete(idempotency_key=key, root_node_id=node.node_id)
    assert not management and source.is_dir()
    assert target.is_symlink() and target.readlink() == destination and not target.exists()
    record = store.get_subtree_delete_record(key)
    assert record.state == "deleting" and record.drained_session_ids == ()
    with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
        assert control.get_fence() == ("active", 1)
    target.unlink()
    assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
    assert management == [node.node_id]
