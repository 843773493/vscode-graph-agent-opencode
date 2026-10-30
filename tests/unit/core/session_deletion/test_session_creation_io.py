"""Session 创建的原同步阶段结算前不能释放同 key 或发布 gate。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core import session_creation as creation
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.test_session_creation import (
    TEST_THREAD_GRAPH_BINDING,
    make_metadata,
)
from tests.unit.core.test_session_creation import service as service  # noqa: PLC0414
from tests.unit.core.test_session_creation import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_session_creation import store as store  # noqa: PLC0414

_STAGES = {
    "freeze": ("prepare", "create_or_get_creation_record"),
    "staging": ("prepare", "_prepare_staging"),
    "prepared-verify": ("prepare", "_verify_session_directory"),
    "publication-read": ("publish", "_require_record_capability"),
    "rename": ("publish", "_rename_staging_to_target"),
    "publish-before": ("publish", "publish_creation_record"),
    "publish-after": ("publish", "publish_creation_record"),
    "published-result": ("recover", "_result_for_published"),
    "abort-read": ("abort", "_require_record_capability"),
    "quarantine": ("abort", "_quarantine_prepared"),
    "abort-after": ("abort", "abort_creation_record"),
}


@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
@pytest.mark.parametrize("pool_wait", [False, True])
async def test_session_creation_settles_original_io_and_recovers_same_identity(
    service: SessionCreationService, store: SessionCatalogStore, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    stage: str, ending: str, pool_wait: bool,
) -> None:
    key = "session-creation-io"
    phase, method = _STAGES[stage]

    async def prepare():
        return await service.prepare(
            idempotency_key=key, title="原 Session 创建", parent_node_id=None,
            graph_binding=TEST_THREAD_GRAPH_BINDING, session_metadata=make_metadata(),
        )

    record = await prepare() if phase != "prepare" else None
    if phase == "recover":
        assert record is not None
        await service.publish_prepared(record)
    owner = store if stage in {"freeze", "publish-before", "publish-after", "abort-after"} else service
    original = getattr(owner, method)
    blockers: list[sqlite3.Connection] = []
    loop_thread = threading.get_ident()

    def pause() -> None:
        if native_step.calls:
            return
        if stage in {"freeze", "publish-before"}:
            blocker = sqlite3.connect(store.database_path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            native_step.calls.append(threading.get_ident())
            native_step.entered.set()
        else:
            native_step.block()

    def wait(*args, **kwargs):
        if stage not in {"publish-after", "abort-after"}:
            pause()
        result = original(*args, **kwargs)
        if stage in {"publish-after", "abort-after"}:
            pause()
        return result

    callback_threads: list[int] = []

    async def confirmed(_record) -> None:
        callback_threads.append(threading.get_ident())

    budget = asyncio.timeout(None)

    async def perform():
        async with budget:
            if phase == "prepare":
                return await prepare()
            assert record is not None
            if phase == "abort":
                return await service.abort_prepared(record, "原操作中止")
            return await service.publish_prepared(record, on_published=confirmed)

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-creation")
    pool_step = NativeStep()
    queued = asyncio.Event()
    tasks: list[Awaitable[object]] = []
    gates = [service._key_lock(key), service._gate.exclusive()]
    if record is not None:
        gates.append(service._session_gate.exclusive(record.session_id))
    entered = [asyncio.Event() for _ in gates]

    async def contend(index: int) -> None:
        async with gates[index]:
            entered[index].set()

    def release() -> None:
        pool_step.release.set()
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, wait)
        if pool_wait:
            worker = creation.run_sync_shielded
            expected_worker = (
                "wait" if stage in {"freeze", "publish-before", "publish-after", "published-result"}
                else {"prepare": "prepare_directory", "publish": "prepare_publication", "abort": "abort"}[phase]
            )

            async def queue_worker(operation, *args, **kwargs):
                if operation.__name__ == expected_worker and not queued.is_set():
                    tasks.append(loop.run_in_executor(None, pool_step.block))
                    await _entered(pool_step)
                    queued.set()
                return await worker(operation, *args, **kwargs)

            patch.setattr(loop, "_default_executor", executor)
            patch.setattr(creation, "run_sync_shielded", queue_worker)
        operating = asyncio.create_task(perform())
        tasks.append(operating)
        try:
            if pool_wait:
                await asyncio.wait_for(queued.wait(), 2)
                assert not native_step.calls
            else:
                await _entered(native_step)
            tasks.extend(asyncio.create_task(contend(index)) for index in range(len(gates)))
            if ending == "cancel":
                operating.cancel("取消原 Session 创建等待")
            elif ending == "deadline":
                budget.reschedule(loop.time())
            await _turns()
            assert not operating.done() and not entered[0].is_set()
            if stage in {"freeze", "publish-before", "publish-after"}:
                assert not any(event.is_set() for event in entered)
            else:
                await asyncio.gather(*tasks[-(len(gates) - 1):])
                assert all(event.is_set() for event in entered[1:])
            assert callback_threads == ([loop_thread] if phase == "recover" else [])
            pool_step.release.set()
            if pool_wait:
                await _entered(native_step)
            release()
            if ending == "cancel":
                with pytest.raises(asyncio.CancelledError, match="取消原 Session 创建等待"):
                    await operating
            elif ending == "deadline":
                with pytest.raises(TimeoutError):
                    await operating
            else:
                await operating
            await asyncio.gather(*tasks, return_exceptions=True)
            assert all(event.is_set() for event in entered)
            assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        finally:
            release()
            await asyncio.gather(*tasks, return_exceptions=True)
            executor.shutdown(wait=True)
            for blocker in blockers:
                blocker.close()

    current = store.get_creation_record(key)
    if record is not None:
        assert (current.session_id, current.main_thread_id, current.storage_relative_locator, current.preimage_hash) == (
            record.session_id, record.main_thread_id, record.storage_relative_locator, record.preimage_hash,
        )
    if phase == "abort":
        assert record is not None
        assert current.state == "aborted"
        assert service._orphaned_dir(current.session_id).is_dir()
        assert await service.abort_prepared(record, "原操作中止") == current
    else:
        published = phase == "recover" or stage in {"publish-before", "publish-after"} or (phase == "publish" and ending == "complete")
        assert current.state == ("published" if published else "preparing")
        assert await prepare() == current
        result = await service.publish_prepared(current)
        assert result.session_id == current.session_id and result.main_thread_id == current.main_thread_id
        assert await service.publish_prepared(current) == result
        assert not service._staging_dir(current.session_id).exists()
        assert (sessions_root / current.storage_relative_locator.removeprefix("sessions/") / "session.json").is_file()
    assert len(store._connection.execute("SELECT session_id FROM session_creation_records").fetchall()) == 1
    assert len(store._connection.execute("SELECT node_id FROM nodes").fetchall()) == (0 if phase == "abort" else 1)
