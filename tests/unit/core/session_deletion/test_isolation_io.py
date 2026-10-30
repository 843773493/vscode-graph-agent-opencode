"""物理隔离完成前保留原删除任务、锁及未提交进度。"""

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


@pytest.mark.parametrize("recover,stage,wait_kind", [
    (False, "mkdir", "native"),
    (False, "rename", "native"),
    (False, "target", "native"),
    (False, "root", "native"),
    (False, "source", "native"),
    (True, "target", "native"),
    (True, "root", "native"),
    (True, "source", "native"),
    (False, "rename", "pool"),
])
@pytest.mark.parametrize("completion", ["done", "cancel", "deadline"])
async def test_isolation_keeps_original_execution_until_io_settles(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    advance_delete_clock: Callable[[float], None], recover: bool, stage: str,
    wait_kind: str, completion: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    peer = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    manifest = (source / "session.json").read_bytes()
    key = "isolation-io-original"
    target = sessions_root / ".deleting" / key / node.node_id
    drains: list[str] = []

    async def drain(session_id: str) -> None:
        drains.append(session_id)

    service.set_session_drain_callback(drain)
    original_mkdir = Path.mkdir
    original_rename, original_barrier = deletion.os.rename, deletion._fsync_directory
    if recover:
        failure = PermissionError(13, "隔离后原屏障失败")

        def fail_barrier(path: Path) -> None:
            if path == target.parent:
                raise failure
            original_barrier(path)

        with monkeypatch.context() as patch:
            patch.setattr(deletion, "_fsync_directory", fail_barrier)
            with pytest.raises(PermissionError) as caught:
                await service.delete(idempotency_key=key, root_node_id=node.node_id)
            assert caught.value.__cause__ is failure
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()

    renames: list[tuple[Path, Path]] = []

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if stage == "mkdir" and path == target.parent and not native_step.calls:
            native_step.block()
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def rename(old: Path, new: Path) -> None:
        if old == source and new == target:
            renames.append((old, new))
            if stage == "rename":
                native_step.block()
        original_rename(old, new)

    barrier_path = {"target": target.parent, "root": target.parent.parent, "source": source.parent}.get(stage)

    def barrier(path: Path) -> None:
        if path == barrier_path and not native_step.calls:
            native_step.block()
        original_barrier(path)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(deletion.os, "rename", rename)
    monkeypatch.setattr(deletion, "_fsync_directory", barrier)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous_executor = getattr(loop, "_default_executor", None)
    executor: ThreadPoolExecutor | None = None
    pool_step = NativeStep()
    queued: asyncio.Future[None] | None = None
    deleting: asyncio.Task[SubtreeDeleteResult] | None = None
    retry: asyncio.Task[SubtreeDeleteResult] | None = None
    gate_waiter: asyncio.Task[None] | None = None
    gate_entered = asyncio.Event()

    async def same_gate() -> None:
        async with service._session_gate.exclusive(node.node_id):
            gate_entered.set()

    try:
        if wait_kind == "pool":
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="isolation-io")
            loop.set_default_executor(executor)
            queued = loop.run_in_executor(None, pool_step.block)
            await _entered(pool_step)
        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        if wait_kind == "native":
            await _entered(native_step)
        else:
            await _turns()
            assert not native_step.calls and not renames
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert record.frozen_session_locators == {node.node_id: node.storage_relative_locator}
        physical = target if recover or stage in ("target", "root", "source") else source
        assert (physical / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(physical / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("deleting", 2)
        if completion == "cancel":
            deleting.cancel("取消原隔离等待")
        elif completion == "deadline":
            advance_delete_clock(31)
        gate_waiter = asyncio.create_task(same_gate())
        retry = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        await _turns()
        assert not deleting.done() and not retry.done() and not gate_entered.is_set()
        async with service._session_gate.exclusive(peer.node_id):
            assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(key) == record
        native_step.release.set()
        pool_step.release.set()
        if completion == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原隔离等待"):
                await deleting
        elif completion == "deadline":
            with pytest.raises(deletion.SubtreeDeleteWaitTimeout, match="phase=drain"):
                await deleting
        else:
            assert (await deleting).record_state == "completed"
        assert (await retry).record_state == "completed"
        await gate_waiter
        assert gate_entered.is_set() and not source.exists()
        assert (target / "session.json").read_bytes() == manifest
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        assert renames == ([] if recover else [(source, target)])
        assert drains == [node.node_id]
        settled = store.get_subtree_delete_record(key)
        assert settled.state == "completed" and settled.drained_session_ids == (node.node_id,)
        assert settled.frozen_session_locators == record.frozen_session_locators
    finally:
        native_step.release.set()
        pool_step.release.set()
        pending: list[Awaitable[object]] = [task for task in (deleting, retry, gate_waiter, queued) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        if executor is not None:
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)
