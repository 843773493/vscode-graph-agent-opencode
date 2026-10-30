"""Child发布、失败收尾与返回前核验的原I/O取消后仍须结算。"""

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
from app.core.thread_creation import ThreadCreationAbortedError, ThreadCreationService
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
    make_session_id,
)
from tests.unit.core.test_thread_creation import owner as owner  # noqa: PLC0414
from tests.unit.core.test_thread_creation import service as service  # noqa: PLC0414
from tests.unit.core.test_thread_creation import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import store as store  # noqa: PLC0414


@pytest.mark.parametrize("stage", [
    "catalog", "publish_before", "publish_after", "recover", "failure_record", "abort", "intent", "terminal",
])
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
@pytest.mark.parametrize("pool_wait", [False, True])
async def test_child_publication_settles_original_io_before_releasing_owner(
    service: ThreadCreationService, owner: OwnerSession, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    stage: str, ending: str, pool_wait: bool,
) -> None:
    key = "child-publication-io"
    control = owner.control
    originals = {name: getattr(control, name) for name in (
        "publish_thread_creation_record", "get_thread_creation_record", "abort_thread_creation_record",
        "get_initial_execution_intent", "mark_thread_creation_published",
    )}
    original_node = service._store.get_node
    catalog_reads = record_reads = 0
    blockers: list[sqlite3.Connection] = []
    failure = RuntimeError("测试原CAS条件冲突")

    def pause(*, sql: bool = False) -> None:
        if native_step.calls:
            return
        if sql:
            blocker = sqlite3.connect(control.database_path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            native_step.calls.append(threading.get_ident())
            native_step.entered.set()
        else:
            native_step.block()

    def release() -> None:
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    def node(*args, **kwargs):
        nonlocal catalog_reads
        catalog_reads += 1
        if stage == "catalog" and catalog_reads == 2:
            pause()
        return original_node(*args, **kwargs)

    def publish(*args, **kwargs):
        if stage in {"failure_record", "abort"}:
            raise failure
        if stage == "publish_before":
            pause(sql=True)
        result = originals["publish_thread_creation_record"](*args, **kwargs)
        if stage == "publish_after":
            pause()
        if stage == "recover":
            raise failure
        return result

    def get_record(*args, **kwargs):
        nonlocal record_reads
        record_reads += 1
        if stage == "recover" or (stage == "failure_record" and record_reads == 2):
            pause()
        return originals["get_thread_creation_record"](*args, **kwargs)

    def abort(*args, **kwargs):
        if stage == "abort":
            pause(sql=True)
        return originals["abort_thread_creation_record"](*args, **kwargs)

    def intent(*args, **kwargs):
        if stage == "intent":
            pause()
        return originals["get_initial_execution_intent"](*args, **kwargs)

    def terminal(*args, **kwargs):
        if stage == "terminal":
            pause()
        return originals["mark_thread_creation_published"](*args, **kwargs)

    monkeypatch.setattr(service._store, "get_node", node)
    for name, operation in zip(originals, (publish, get_record, abort, intent, terminal), strict=True):
        monkeypatch.setattr(control, name, operation)
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
            selected = (
                (stage in {"catalog", "publish_before", "publish_after"} and operation.__name__ == "publish")
                or (stage == "recover" and operation == get_record)
                or (stage == "failure_record" and operation == get_record and record_reads == 1)
                or (stage == "abort" and operation == abort)
                or (stage == "intent" and operation == intent)
                or (stage == "terminal" and operation == terminal)
            )
            if selected and not queued.is_set():
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
            return await do_create(service, owner, key=key)

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
            creating.cancel("取消原child发布等待")
        elif ending == "deadline":
            budget.reschedule(loop.time())
        await _turns()
        assert not creating.done() and service._key_lock(key).locked() and not entered[2].is_set()
        if stage in {"catalog", "publish_before", "publish_after", "recover"}:
            assert not entered[0].is_set() and not entered[1].is_set()
        else:
            await asyncio.gather(*tasks[-3:-1])
            assert entered[0].is_set() and entered[1].is_set()
        async with service._session_gate.exclusive(make_session_id()):
            assert not creating.done()
        pool_step.release.set()
        if pool_wait:
            await _entered(native_step)
        release()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原child发布等待"):
                await creating
        elif ending == "deadline":
            with pytest.raises(TimeoutError):
                await creating
        elif stage in {"failure_record", "abort"}:
            with pytest.raises(RuntimeError, match="CAS 失败") as caught:
                await creating
            assert caught.value.__cause__ is failure
        else:
            assert (await creating).record_state == "published"
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(event.is_set() for event in entered)
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        with closing(SessionControlStore(control.database_path, read_only=True)) as reader:
            frozen = reader.get_thread_creation_record(key)
            if stage == "abort" or (stage == "failure_record" and ending == "complete"):
                assert frozen.state == "aborted" and not reader.list_child_thread_rows()
                with pytest.raises(ThreadCreationAbortedError):
                    await do_create(service, owner, key=key)
            else:
                assert frozen.state == ("preparing" if stage == "failure_record" else "published")
                monkeypatch.setattr(control, "publish_thread_creation_record", originals["publish_thread_creation_record"])
                result = await do_create(service, owner, key=key)
                assert result.child_thread_id == frozen.child_thread_id
                assert result.final_relative_locator == frozen.final_relative_locator
                assert len(reader.list_child_thread_rows()) == 1
                assert reader.get_thread_creation_record(key).preimage_hash == frozen.preimage_hash
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
