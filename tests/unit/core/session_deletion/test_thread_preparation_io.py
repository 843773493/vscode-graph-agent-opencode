"""冻结后的 child 目录 I/O 必须结算原调用，取消不遗弃 holder 或继续发布。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app.core.thread_creation as creation
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
from tests.unit.core.test_thread_creation import OwnerSession, do_create, make_metadata
from tests.unit.core.test_thread_creation import owner as owner  # noqa: PLC0414
from tests.unit.core.test_thread_creation import service as service  # noqa: PLC0414
from tests.unit.core.test_thread_creation import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import store as store  # noqa: PLC0414


@pytest.mark.parametrize("stage", ["staging", "rename", "recovered", "published"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
@pytest.mark.parametrize("pool_wait", [False, True])
async def test_child_directory_io_settles_before_original_holder_is_released(
    service: ThreadCreationService, owner: OwnerSession, sessions_root: Path,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    stage: str, ending: str, pool_wait: bool,
) -> None:
    key = "child-preparation-io"
    metadata = make_metadata(task_seed=None, task_reference=None)

    async def create():
        return await do_create(service, owner, key=key, initial_state="idle", metadata=metadata)

    if stage == "recovered":
        async def interrupt(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("模拟rename后尚未发布")

        with monkeypatch.context() as patch:
            patch.setattr(service, "_publish_and_finalize", interrupt)
            with pytest.raises(RuntimeError, match="模拟rename后"):
                await create()

    operation = {
        "staging": service._prepare_staging,
        "rename": service._rename_staging_to_final,
        "recovered": creation.verify_node_directory,
        "published": creation.verify_node_directory,
    }[stage]
    original_worker = creation.run_sync_shielded
    loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=1)
    pool_step = NativeStep()
    queued = asyncio.Event()
    tasks: list[Awaitable[object]] = []

    def selected(candidate, kwargs) -> bool:
        if candidate != operation:
            return False
        if stage == "recovered":
            return kwargs.get("stage") == "恢复(最终 locator 已存在)"
        if stage == "published":
            return kwargs.get("stage") == "published 复验"
        return True

    def wait(*args, **kwargs):
        native_step.block()
        return operation(*args, **kwargs)

    # 故障点在原同步函数内部，worker调度路径仍由生产入口决定。
    if stage in {"staging", "rename"}:
        monkeypatch.setattr(service, "_prepare_staging" if stage == "staging" else "_rename_staging_to_final", wait)
    else:
        def verify(*args, **kwargs):
            if selected(operation, kwargs):
                return wait(*args, **kwargs)
            return operation(*args, **kwargs)
        monkeypatch.setattr(creation, "verify_node_directory", verify)

    # 只在原目标步骤投递前占满线程池，不能挡住前序目录探测。
    if pool_wait:
        target = service._prepare_staging if stage == "staging" else service._rename_staging_to_final if stage == "rename" else creation.verify_node_directory

        async def queue_worker(candidate, *args, **kwargs):
            if candidate == target and (stage in {"staging", "rename"} or selected(operation, kwargs)) and not queued.is_set():
                tasks.append(loop.run_in_executor(None, pool_step.block))
                await _entered(pool_step)
                queued.set()
            return await original_worker(candidate, *args, **kwargs)

        monkeypatch.setattr(creation, "run_sync_shielded", queue_worker)
        loop.set_default_executor(executor)

    budget = asyncio.timeout(None)
    key_entered, holder_entered = asyncio.Event(), asyncio.Event()
    holders = OperationHolderGate(sessions_root, namespace="thread_creation")

    async def create_with_budget():
        async with budget:
            return await create()

    async def same_key() -> None:
        async with service._key_lock(key):
            key_entered.set()

    async def same_holder() -> None:
        async with holders.lock_for(f"{owner.session_id}/{key}"):
            holder_entered.set()

    creating = asyncio.create_task(create_with_budget())
    tasks.append(creating)
    try:
        if pool_wait:
            await asyncio.wait_for(queued.wait(), 2)
            assert not native_step.calls
        else:
            await _entered(native_step)
        frozen = owner.control.get_thread_creation_record(key)
        assert frozen.state == ("published" if stage == "published" else "preparing")
        tasks.extend([asyncio.create_task(same_key()), asyncio.create_task(same_holder())])
        if ending == "cancel":
            creating.cancel("取消原child目录等待")
        elif ending == "deadline":
            budget.reschedule(loop.time())
        await _turns()
        assert not creating.done() and not key_entered.is_set() and not holder_entered.is_set()
        async with service._gate.exclusive(), service._session_gate.exclusive(owner.session_id):
            assert owner.control.get_thread_creation_record(key) == frozen
        native_step.release.set()
        pool_step.release.set()
        if ending == "complete":
            assert (await creating).record_state == "published"
        elif ending == "cancel":
            with pytest.raises(asyncio.CancelledError, match="取消原child目录等待"):
                await creating
        else:
            with pytest.raises(TimeoutError):
                await creating
        await asyncio.gather(*tasks, return_exceptions=True)
        assert key_entered.is_set() and holder_entered.is_set()
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        after = owner.control.get_thread_creation_record(key)
        assert (after.child_thread_id, after.final_relative_locator, after.artifact_manifest, after.preimage_hash) == (
            frozen.child_thread_id, frozen.final_relative_locator, frozen.artifact_manifest, frozen.preimage_hash,
        )
        if ending != "complete":
            assert after == frozen
        result = await create()
        assert result.child_thread_id == frozen.child_thread_id and result.record_state == "published"
        assert len(owner.control.list_child_thread_rows()) == 1
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
