"""从产品执行生成 runtime 关联，验证对账 worker、代次竞争和取消结算。"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.core.resource_uri import parse_attachment_uri
from app.core.trace_middleware import (
    get_current_request_id,
    reset_current_request_id,
    set_current_request_id,
)
from app.schemas.internal_v2.job import JobStatus
from app.schemas.internal_v2.message import (
    AttachmentRef,
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.services.business.job.service import JobService
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.integration.backend.sessions.test_attachment_runtime_lifecycle import (
    attachment_directory,
    execute_upload,
    isolate_unused_external_managers,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@dataclass(frozen=True)
class BindingCase:
    runtime: RuntimeCase
    session_id: str
    thread_id: str
    attachment_id: str
    ref: AttachmentRef
    token: str


@pytest.fixture
async def binding_case(runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch) -> BindingCase:
    isolate_unused_external_managers(runtime_case, monkeypatch)
    session_id, thread_id, ref = await execute_upload(runtime_case)
    attachment_id = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert attachment_id is not None
    token = runtime_case.tracker.runtime_binding_token(runtime_case.tracker.snapshot(session_id, thread_id).generation)
    return BindingCase(runtime_case, session_id, thread_id, attachment_id, ref, token)


@pytest.mark.parametrize("stage", ["bindings", "control", "queue", "release"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel_failure"])
async def test_reconcile_storage_keeps_loop_context_and_original_outcome(
    binding_case: BindingCase, monkeypatch: pytest.MonkeyPatch, stage: str, outcome: str,
) -> None:
    case = binding_case
    container = case.runtime.runner.container
    store = container.attachment_blob_store
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    expired: list[bool] = []
    failure = OSError("附件运行时对账访问失败")
    owner, method = {
        "bindings": (store._catalog, "runtime_bindings"),
        "control": (store, "_open_control_store"),
        "queue": (container.checkpointer, "execution_queue_snapshot"),
        "release": (store._catalog, "release_associations"),
    }[stage]
    original = getattr(owner, method)
    calls = 0
    cold_tracker = ThreadResidencyTracker()

    def block(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert get_current_request_id() == "attachment-reconcile-request"
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                expired.append(True)
                raise TimeoutError("附件对账阻塞事件循环")
            assert threading.get_ident() != event_thread
            if "failure" in outcome:
                raise failure
        return original(*args, **kwargs)

    def is_current(session_id: str, thread_id: str, token: str) -> bool:
        assert threading.get_ident() == event_thread
        return cold_tracker.is_current_runtime_binding(session_id, thread_id, token)

    async def reconcile() -> tuple[str, ...]:
        return await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=is_current)

    request = set_current_request_id("attachment-reconcile-request")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, block)
            pending = asyncio.create_task(reconcile())
            try:
                await asyncio.wait_for(entered.wait(), 7)
                assert not expired and not pending.done()
                async with store.deletion_holder.lock_for(case.session_id, wait=False) as holder:
                    assert holder.acquired is (stage == "bindings")
                if "cancel" in outcome:
                    pending.cancel("取消附件对账")
                    await asyncio.sleep(0)
                    pending.cancel("重复取消")
                    await asyncio.sleep(0)
                    assert not pending.done()
                    async with store.deletion_holder.lock_for(case.session_id, wait=False) as holder:
                        assert holder.acquired is (stage == "bindings")
                release.set()
                if "failure" in outcome:
                    with pytest.raises(OSError) as caught:
                        await pending
                    assert caught.value is failure
                    if "cancel" in outcome:
                        assert isinstance(failure.__context__, asyncio.CancelledError)
                        assert failure.__context__.args == ("取消附件对账",)
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="取消附件对账"):
                        await pending
                else:
                    assert await pending == (case.attachment_id,)
                retained = "failure" in outcome or (stage == "bindings" and outcome == "cancel")
                record = store._catalog.get_directory(case.attachment_id)
                assert record is not None
                pending_release = not retained and outcome == "cancel"
                assert record.availability == ("available" if retained or pending_release else "released")
                assert bool(record.release_pending) is pending_release
                assert attachment_directory(store, case.attachment_id).exists() is (retained or pending_release)
                assert store._catalog.runtime_bindings() == (((case.session_id, case.thread_id, case.token),) if retained else ())
                assert await reconcile() == ((case.attachment_id,) if retained else ())
                assert await reconcile() == ()
                assert await store.process_pending_directories() == ((case.attachment_id,) if pending_release else ())
                assert not attachment_directory(store, case.attachment_id).exists()
                assert store.read(case.session_id, case.ref.file_id).data == b"runtime original"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        reset_current_request_id(request)


@pytest.mark.parametrize("fail_callback", [False, True])
async def test_current_generation_is_checked_on_event_loop_before_any_release(
    binding_case: BindingCase, monkeypatch: pytest.MonkeyPatch, fail_callback: bool,
) -> None:
    case = binding_case
    container = case.runtime.runner.container
    store = container.attachment_blob_store
    event_thread = threading.get_ident()
    failure = RuntimeError("原 tracker 校验失败")
    seen: list[tuple[str, str, str]] = []

    def is_current(session_id: str, thread_id: str, token: str) -> bool:
        assert threading.get_ident() == event_thread
        seen.append((session_id, thread_id, token))
        if fail_callback:
            raise failure
        return case.runtime.tracker.is_current_runtime_binding(session_id, thread_id, token)

    def unexpected_release(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("当前代不得进入存储解绑")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_open_control_store", unexpected_release)
        if fail_callback:
            with pytest.raises(RuntimeError) as caught:
                await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=is_current)
            assert caught.value is failure
        else:
            assert await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=is_current) == ()
        assert seen == [(case.session_id, case.thread_id, case.token)]
        assert store._catalog.runtime_bindings() == tuple(seen)
        assert not store._catalog.require_available_directory(case.attachment_id).release_pending


async def test_old_worker_cannot_release_generation_created_during_storage_wait(
    binding_case: BindingCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = binding_case
    container = case.runtime.runner.container
    store = container.attachment_blob_store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = store._catalog.release_associations
    calls = 0

    def delayed_release(*, session_id: str, thread_id: str | None = None,
                        binding_token: str | None = None, now: datetime) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert binding_token == case.token
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(30):
                raise TimeoutError("旧代解绑屏障未释放")
        return original(session_id=session_id, thread_id=thread_id, binding_token=binding_token, now=now)

    cold_tracker = ThreadResidencyTracker()
    with monkeypatch.context() as patch:
        patch.setattr(store._catalog, "release_associations", delayed_release)
        pending = asyncio.create_task(store.reconcile_runtime_bindings(
            messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 12)
            # 原代正常卸载，再经正式消息执行重建 runtime 与本目录关联；不手填新 token。
            await case.runtime.tracker.close(session_id=case.session_id)
            result = await container.session_orchestrator.create_message(case.session_id, MessageRunRequest(
                message=MessageCreateRequest(content="再次使用同一附件", attachments=[case.ref]), run=RunOptions(),
            ))
            assert isinstance(container.job_service, JobService)
            job = container.job_service._jobs[result.job_id]
            assert job.task is not None

            async def new_binding() -> str:
                while True:
                    bindings = store._catalog.runtime_bindings()
                    if bindings and bindings != ((case.session_id, case.thread_id, case.token),):
                        assert len(bindings) == 1 and bindings[0][:2] == (case.session_id, case.thread_id)
                        return bindings[0][2]
                    await asyncio.sleep(0.01)

            new_token = await asyncio.wait_for(new_binding(), 15)
            generation = case.runtime.tracker.snapshot(case.session_id, case.thread_id).generation
            assert new_token == case.runtime.tracker.runtime_binding_token(generation)
            assert new_token != case.token and len(case.runtime.compiled) == 2
            # 新代关联已发布就交还旧回执，不把后续模型准备时间算进故障屏障。
            release.set()
            assert await pending == ()
            await asyncio.wait_for(asyncio.shield(job.task), 15)
            assert job.status == JobStatus.completed, job.error_message
            assert store._catalog.runtime_bindings() == ((case.session_id, case.thread_id, new_token),)
            assert not store._catalog.require_available_directory(case.attachment_id).release_pending
            assert store.read(case.session_id, case.ref.file_id).data == b"runtime original"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_unload_failure_keeps_same_generation_binding_until_owner_retries(
    binding_case: BindingCase,
) -> None:
    case = binding_case
    container = case.runtime.runner.container
    store, tracker = container.attachment_blob_store, case.runtime.tracker
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("卸载在附件解绑前失败")

    async def fail_unload(session_id: str, thread_id: str, generation: int) -> None:
        assert (session_id, thread_id) == (case.session_id, case.thread_id)
        assert tracker.runtime_binding_token(generation) == case.token
        entered.set()
        await release.wait()
        raise failure

    tracker._unload_listeners.insert(0, fail_unload)
    pending = asyncio.create_task(tracker.close(session_id=case.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not tracker.is_current_generation(case.session_id, case.thread_id, tracker.snapshot(case.session_id, case.thread_id).generation)
        # closing 撤销准入，但在原 runtime 移除前不能把其附件当作旧代孤儿。
        assert tracker.is_current_runtime_binding(case.session_id, case.thread_id, case.token)
        assert await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=tracker.is_current_runtime_binding) == ()
        release.set()
        with pytest.raises(ExceptionGroup) as caught:
            await pending
        assert failure in caught.value.exceptions
        assert tracker.is_current_runtime_binding(case.session_id, case.thread_id, case.token)
        assert store._catalog.runtime_bindings() == ((case.session_id, case.thread_id, case.token),)
        assert not store._catalog.require_available_directory(case.attachment_id).release_pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        tracker._unload_listeners.remove(fail_unload)
    await tracker.close(session_id=case.session_id)
    assert not tracker.is_current_runtime_binding(case.session_id, case.thread_id, case.token)
    assert store._catalog.runtime_bindings() == ()
    released = store._catalog.get_directory(case.attachment_id)
    assert released is not None and released.availability == "released"
    assert not attachment_directory(store, case.attachment_id).exists()


async def test_stale_snapshot_does_not_reopen_control_after_owner_released_binding(
    binding_case: BindingCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = binding_case
    container = case.runtime.runner.container
    store = container.attachment_blob_store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    snapshot = store._catalog.runtime_bindings
    calls = 0

    def frozen_snapshot() -> tuple[tuple[str, str, str], ...]:
        nonlocal calls
        frozen = snapshot()
        calls += 1
        if calls == 1:
            assert frozen == ((case.session_id, case.thread_id, case.token),)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(15):
                raise TimeoutError("旧关联快照屏障未释放")
        return frozen

    def unexpected_control(_session_id: str) -> None:
        raise AssertionError("原关联已释放后，不得因旧快照重新打开可能已隔离的控制库")

    cold_tracker = ThreadResidencyTracker()
    with monkeypatch.context() as patch:
        patch.setattr(store._catalog, "runtime_bindings", frozen_snapshot)
        pending = asyncio.create_task(store.reconcile_runtime_bindings(
            messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 17)
            await case.runtime.tracker.close(session_id=case.session_id)
            assert store._catalog.runtime_bindings() == ()
            with monkeypatch.context() as no_control:
                no_control.setattr(store, "_open_control_store", unexpected_control)
                release.set()
                assert await pending == ()
            released = store._catalog.get_directory(case.attachment_id)
            assert released is not None and released.availability == "released"
            assert not attachment_directory(store, case.attachment_id).exists()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
