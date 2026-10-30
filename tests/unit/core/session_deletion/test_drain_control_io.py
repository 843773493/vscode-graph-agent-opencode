"""删除收敛控制库的打开/关闭必须结算，取消不得泄漏未交付连接。"""

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

import app.core.session_subtree_delete as deletion
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
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


@dataclass
class ControlWait:
    step: NativeStep
    opened: list[SessionControlStore] = field(default_factory=list)
    closed: list[SessionControlStore] = field(default_factory=list)
    close_threads: list[int] = field(default_factory=list)
    blockers: list[sqlite3.Connection] = field(default_factory=list)

    def release(self) -> None:
        self.step.release.set()
        for blocker in self.blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")


@pytest.fixture
def control_wait(
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Path, str], ControlWait]]:
    probes: list[ControlWait] = []

    def install(database: Path, stage: str) -> ControlWait:
        probe = ControlWait(native_step)
        probes.append(probe)

        class Control(SessionControlStore):
            def __init__(self, database_path: Path, *, read_only: bool = False) -> None:
                super().__init__(database_path, read_only=read_only)
                if self.database_path == database:
                    probe.opened.append(self)

            def _begin_immediate(self) -> None:
                if stage == "open" and self.database_path == database and not probe.step.calls:
                    blocker = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
                    blocker.execute("BEGIN IMMEDIATE")
                    probe.blockers.append(blocker)
                    probe.step.calls.append(threading.get_ident())
                    probe.step.entered.set()
                super()._begin_immediate()

            def close(self) -> None:
                tracked = self.database_path == database and not self._closed
                if tracked:
                    probe.close_threads.append(threading.get_ident())
                    if stage == "close" and not probe.step.calls:
                        probe.step.block()
                super().close()
                if tracked:
                    probe.closed.append(self)

        monkeypatch.setattr(deletion, "SessionControlStore", Control)
        return probe

    yield install
    for probe in probes:
        probe.release()
        for blocker in probe.blockers:
            blocker.close()
        for owner in probe.opened:
            SessionControlStore.close(owner)


@pytest.mark.parametrize("stage", ["open", "close"])
@pytest.mark.parametrize("wait_kind", ["native", "pool"])
@pytest.mark.parametrize("completion", ["done", "cancel", "deadline"])
async def test_delete_waits_for_original_control_access(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, control_wait: Callable[[Path, str], ControlWait],
    monkeypatch: pytest.MonkeyPatch, advance_delete_clock: Callable[[float], None],
    stage: str, wait_kind: str, completion: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    peer = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    manifest = (source / "session.json").read_bytes()
    key = "delete-control-io"
    target = sessions_root / ".deleting" / key / node.node_id
    probe = control_wait(source / "session-control.sqlite", stage)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous_executor = getattr(loop, "_default_executor", None)
    executor: ThreadPoolExecutor | None = None
    pool_step = NativeStep()
    queued: asyncio.Future[None] | None = None
    deleting: asyncio.Task[SubtreeDeleteResult] | None = None
    pool_queued = asyncio.Event()
    list_pending = SessionControlStore.list_pending_thread_creations

    def pending(control: SessionControlStore) -> tuple[ThreadCreationRecord, ...]:
        nonlocal queued
        result = list_pending(control)
        if wait_kind == "pool" and stage == "close" and queued is None:
            queued = loop.run_in_executor(None, pool_step.block)
            pool_queued.set()
        return result

    monkeypatch.setattr(SessionControlStore, "list_pending_thread_creations", pending)
    try:
        if wait_kind == "pool":
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete-control")
            loop.set_default_executor(executor)
            if stage == "open":
                queued = loop.run_in_executor(None, pool_step.block)
                pool_queued.set()
        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        if wait_kind == "native":
            await _entered(probe.step)
        else:
            await asyncio.wait_for(pool_queued.wait(), 2)
            await _entered(pool_step)
            await _turns()
            assert not probe.step.calls
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert record.frozen_session_locators == {node.node_id: node.storage_relative_locator}
        assert (source / "session.json").read_bytes() == manifest and not target.exists()
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
        if stage == "close":
            assert len(probe.opened) == 1 and not probe.closed
        if completion == "cancel":
            deleting.cancel("取消原控制库等待")
        elif completion == "deadline":
            advance_delete_clock(31)
        await _turns()
        assert not deleting.done() and service._key_lock(key).locked()
        async with service._session_gate.exclusive(peer.node_id):
            assert store.get_node(peer.node_id).state == "active"
        assert store.get_subtree_delete_record(key) == record
        if wait_kind == "pool":
            pool_step.release.set()
            await _entered(probe.step)
        probe.release()
        if completion == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原控制库等待"):
                await deleting
        elif completion == "deadline":
            with pytest.raises(deletion.SubtreeDeleteWaitTimeout, match="phase=drain"):
                await deleting
        else:
            assert (await deleting).record_state == "completed"
        first = probe.opened[0]
        assert probe.closed.count(first) == 1
        with pytest.raises(RuntimeError, match="已关闭"):
            _ = first.connection
        assert len(probe.step.calls) == 1 and probe.step.calls[0] != loop_thread
        assert all(thread_id != loop_thread for thread_id in probe.close_threads)
        if completion != "done":
            assert store.get_subtree_delete_record(key) == record
            assert (source / "session.json").read_bytes() == manifest and not target.exists()
            assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
    finally:
        probe.release()
        pool_step.release.set()
        pending_tasks: list[Awaitable[object]] = [task for task in (deleting, queued) if task is not None]
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        if executor is not None:
            if previous_executor is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)


@pytest.mark.parametrize("stage", ["open", "settle", "close"])
async def test_control_failure_keeps_original_error_connection_and_retry(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    assert node.storage_relative_locator is not None
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    database = source / "session-control.sqlite"
    manifest = (source / "session.json").read_bytes()
    key = "control-failure-retry"
    failure = RuntimeError("原控制库收敛故障")
    failed = False
    closed: list[SessionControlStore] = []
    close_threads: list[int] = []

    class Control(SessionControlStore):
        def _fail(self, current_stage: str) -> None:
            nonlocal failed
            if stage == current_stage and self.database_path == database and not failed:
                failed = True
                raise failure

        def _begin_immediate(self) -> None:
            self._fail("open")
            super()._begin_immediate()

        def list_pending_thread_creations(self) -> tuple[ThreadCreationRecord, ...]:
            self._fail("settle")
            return super().list_pending_thread_creations()

        def close(self) -> None:
            if self.database_path == database and not self._closed:
                closed.append(self)
                close_threads.append(threading.get_ident())
            super().close()
            self._fail("close")

    monkeypatch.setattr(deletion, "SessionControlStore", Control)
    with pytest.raises(RuntimeError) as caught:
        await service.delete(idempotency_key=key, root_node_id=node.node_id)
    assert caught.value is failure and failed
    assert len(closed) == 1 and close_threads[0] != threading.get_ident()
    with pytest.raises(RuntimeError, match="已关闭"):
        _ = closed[0].connection
    record = store.get_subtree_delete_record(key)
    assert record.state == "deleting" and record.drained_session_ids == ()
    assert (source / "session.json").read_bytes() == manifest
    with closing(SessionControlStore(database, read_only=True)) as control:
        assert control.get_fence() == ("active", 1)
    assert not service._key_lock(key).locked()
    assert (await service.delete(idempotency_key=key, root_node_id=node.node_id)).record_state == "completed"
    assert closed.count(closed[0]) == 1
    target = sessions_root / ".deleting" / key / node.node_id
    assert not source.exists() and (target / "session.json").read_bytes() == manifest
