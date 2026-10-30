"""Child准入的目录核对和SQLite冻结等待必须结算后才释放原gate。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

import app.core.thread_creation as creation
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.thread_creation import ThreadCreationService
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import (
    OwnerSession,
    do_create,
    make_metadata,
    make_session_id,
)
from tests.unit.core.test_thread_creation import owner as owner  # noqa: PLC0414
from tests.unit.core.test_thread_creation import service as service  # noqa: PLC0414
from tests.unit.core.test_thread_creation import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import store as store  # noqa: PLC0414


@pytest.mark.parametrize("stage", ["catalog", "binding", "main", "record", "manifest"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
@pytest.mark.parametrize("pool_wait", [False, True])
async def test_child_admission_settles_before_releasing_original_gates(
    service: ThreadCreationService, owner: OwnerSession, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    stage: str, ending: str, pool_wait: bool,
) -> None:
    key = "child-admission-io"
    metadata = make_metadata(task_seed=None, task_reference=None)
    target, name = {
        "catalog": (service._store, "get_node"),
        "binding": (service, "_validate_control_store_binding"),
        "main": (owner.control, "verify_matches_catalog_main_thread"),
        "record": (owner.control, "create_or_get_thread_creation_record"),
        "manifest": (owner.control, "freeze_thread_creation_artifact_manifest"),
    }[stage]
    original = getattr(target, name)
    blockers: list[sqlite3.Connection] = []

    def wait(*args, **kwargs):
        if not native_step.calls:
            if stage in {"record", "manifest"}:
                blocker = sqlite3.connect(owner.control.database_path, isolation_level=None, check_same_thread=False)
                blocker.execute("BEGIN IMMEDIATE")
                blockers.append(blocker)
                native_step.calls.append(threading.get_ident())
                native_step.entered.set()
            else:
                native_step.block()
        return original(*args, **kwargs)

    def release() -> None:
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    monkeypatch.setattr(target, name, wait)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    previous = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=1)
    pool_step = NativeStep()
    queued = asyncio.Event()
    tasks: list[Awaitable[object]] = []
    worker = creation.run_sync_shielded

    if pool_wait:
        async def queue_worker(operation, *args, **kwargs):
            if not queued.is_set():
                tasks.append(loop.run_in_executor(None, pool_step.block))
                await _entered(pool_step)
                queued.set()
            return await worker(operation, *args, **kwargs)

        monkeypatch.setattr(creation, "run_sync_shielded", queue_worker)
        loop.set_default_executor(executor)

    holders = OperationHolderGate(sessions_root, namespace="thread_creation")
    gates = [service._gate.exclusive(), service._session_gate.exclusive(owner.session_id),
             holders.lock_for(f"{owner.session_id}/{key}")]
    entered = [asyncio.Event() for _ in gates]
    budget = asyncio.timeout(None)

    async def create():
        async with budget:
            return await do_create(service, owner, key=key, initial_state="idle", metadata=metadata)

    async def contend(index: int) -> None:
        async with gates[index]:
            entered[index].set()

    creating = asyncio.create_task(create())
    tasks.append(creating)
    try:
        if pool_wait:
            await asyncio.wait_for(queued.wait(), 2)
            assert not native_step.calls
        else:
            await _entered(native_step)
        tasks.extend(asyncio.create_task(contend(index)) for index in range(len(gates)))
        if ending == "cancel":
            creating.cancel("取消原child准入等待")
        elif ending == "deadline":
            budget.reschedule(loop.time())
        await _turns()
        assert not creating.done() and service._key_lock(key).locked()
        assert not any(event.is_set() for event in entered)
        # 不同Session的gate仍可进入；同Session原准入不能被取消提前放行。
        async with service._session_gate.exclusive(make_session_id()):
            assert not (owner.session_dir / ".staging" / key).exists()
        pool_step.release.set()
        if pool_wait:
            await _entered(native_step)
        release()
        if ending == "complete":
            assert (await creating).record_state == "published"
        elif ending == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原child准入等待"):
                await creating
        else:
            with pytest.raises(TimeoutError):
                await creating
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(event.is_set() for event in entered)
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        with closing(SessionControlStore(owner.control.database_path, read_only=True)) as control:
            frozen = control.get_thread_creation_record(key)
            assert frozen.artifact_manifest is not None and frozen.abort_reason is None
            if ending != "complete":
                assert frozen.state == "preparing" and not control.list_child_thread_rows()
                assert not (owner.session_dir / ".staging" / key).exists()
        result = await do_create(service, owner, key=key, initial_state="idle", metadata=metadata)
        assert result.child_thread_id == frozen.child_thread_id
        assert result.final_relative_locator == frozen.final_relative_locator
        assert len(owner.control.list_child_thread_rows()) == 1
        published = owner.control.get_thread_creation_record(key)
        assert (published.preimage_hash, published.artifact_manifest) == (frozen.preimage_hash, frozen.artifact_manifest)
        assert len(native_step.calls) == 1
    finally:
        release()
        pool_step.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for blocker in blockers:
            blocker.close()
        if pool_wait:
            if previous is None:
                monkeypatch.setattr(loop, "_default_executor", None)
            else:
                loop.set_default_executor(previous)
        executor.shutdown(wait=True)
