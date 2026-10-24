"""从真实消息和图执行生成目录关联，验证卸载、冷 owner 与排队输入。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.resource_uri import parse_attachment_uri
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.job import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.support.message_service import accept_queued_message


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 24, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def execute_upload(case: RuntimeCase):
    container = case.runner.container
    session = await container.session_service.create(SessionCreateRequest(title="目录运行时释放"))
    result = await container.session_orchestrator.create_message(session.session_id, MessageRunRequest(
        message=MessageCreateRequest(content="读取本次上传", attachments=[upload(b"runtime original")]),
        run=RunOptions(),
    ))
    job = container.job_service._jobs[result.job_id]
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
    assert job.status == JobStatus.completed, job.error_message
    ref, = job.attachments
    return session.session_id, result.thread_id, ref


def isolate_unused_external_managers(case: RuntimeCase, monkeypatch: pytest.MonkeyPatch) -> None:
    for owner in (case.runner.container.terminal_resource_owner, case.runner.container.browser_resource_owner):
        monkeypatch.setattr(owner._transport, "list_resources", AsyncMock(return_value=[]))


async def test_real_graph_unload_starts_thirty_days_and_historical_uri_does_not_retain(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    store = case.runner.container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert len(case.compiled) == 1
    generation = case.tracker.snapshot(sid, tid).generation
    token = case.tracker.runtime_binding_token(generation)
    assert store._catalog.runtime_bindings() == ((sid, tid, token),)
    assert not case.runner.saver.execution_queue_snapshot(sid, tid).records
    await case.tracker.close(session_id=sid)
    record = store._catalog.get_directory(aid)
    assert record.unattached_since == clock().isoformat()
    assert record.gc_after == (clock() + timedelta(days=30)).isoformat()
    clock.advance(29)
    assert await store.collect_garbage(leases=case.runner.container.external_resource_leases) == ()
    clock.advance(1)
    assert await store.collect_garbage(leases=case.runner.container.external_resource_leases) == (aid,)
    assert store._catalog.get_directory(aid).availability == "tombstoned"
    with pytest.raises(FileNotFoundError):
        store.read(sid, ref.file_id)
    assert case.runner.saver.attachment_reference_in_use(sid, tid, aid)


async def test_cold_owner_releases_crash_binding_without_resetting_deadline(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    store = case.runner.container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    _sid, _tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    cold_tracker = ThreadResidencyTracker()
    with closing(AttachmentBlobStore(case.runner.workspace, path_resolver=case.runner.container.session_service.path_resolver, clock=clock)) as cold:
        assert cold.reconcile_runtime_bindings(messages=case.runner.saver, is_current=cold_tracker.is_current_runtime_binding) == (aid,)
        deadline = cold._catalog.get_directory(aid).gc_after
        clock.advance(12)
        assert cold.reconcile_runtime_bindings(messages=case.runner.saver, is_current=cold_tracker.is_current_runtime_binding) == ()
        assert cold._catalog.get_directory(aid).gc_after == deadline


@pytest.mark.parametrize("accepted", [False, True])
async def test_existing_uri_input_has_pin_before_accept_and_queue_survives_cold_owner(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch, accepted: bool,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    await case.tracker.close(session_id=sid)
    clock.advance(29)
    message = await container.message_service.create(sid, MessageCreateRequest(content="再次使用 URI", attachments=[ref]))
    with closing(store._open_control_store(sid)) as control:
        pin, = control.list_non_terminal_leases()
        assert pin.operation_identity.startswith("attref_") and pin.state == "active"
    assert store._catalog.get_directory(aid).gc_after is None
    if accepted:
        accept_queued_message(container.message_service, message)
    cold_tracker = ThreadResidencyTracker()
    with closing(AttachmentBlobStore(case.runner.workspace, path_resolver=container.session_service.path_resolver, clock=clock)) as cold:
        await cold.recover_idle_session(sid, messages=container.checkpointer)
        assert cold.reconcile_runtime_bindings(messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding) == ()
        with closing(cold._open_control_store(sid)) as control:
            assert not control.list_non_terminal_leases()
        clock.advance(31)
        assert await cold.collect_garbage(leases=container.external_resource_leases) == (() if accepted else (aid,))
        if accepted:
            assert cold.read(sid, ref.file_id).data == b"runtime original"
            assert container.checkpointer.execution_queue_snapshot(sid, tid).records


async def test_never_resident_queued_upload_is_not_a_crash_orphan(startup_container) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="首次排队"))
    message = await container.message_service.create(session.session_id, MessageCreateRequest(content="尚未执行", attachments=[upload(b"cold queued")]))
    accept_queued_message(container.message_service, message)
    tracker = ThreadResidencyTracker()
    store = container.attachment_blob_store
    assert tracker.snapshot(session.session_id, message.thread_id).residency == "cold"
    assert store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=tracker.is_current_runtime_binding) == ()
    assert store.read(session.session_id, message.attachments[0].file_id).data == b"cold queued"
    aid = parse_attachment_uri(message.attachments[0].file_id, require_file=True).attachment_id
    assert store._catalog.get_directory(aid).gc_after is None


async def test_queued_input_retains_previous_runtime_then_old_release_cannot_detach_new_generation(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    old_token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    queued = await container.message_service.create(sid, MessageCreateRequest(content="使用此前的附件"))
    accept_queued_message(container.message_service, queued)
    record, = container.checkpointer.execution_queue_snapshot(sid, tid).records
    await case.tracker.close(session_id=sid)
    assert store._catalog.runtime_bindings() == ((sid, tid, old_token),)
    cold_tracker = ThreadResidencyTracker()
    assert store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding) == ()
    clock.advance(31)
    assert await store.collect_garbage(leases=container.external_resource_leases) == ()
    result = await container.job_service.start_job(
        sid, queued.content, thread_id=tid, message_id=queued.message_id,
        message_created_at=queued.created_at.isoformat(),
        message_metadata={"boxteam_session_provider_id": record.input.provider_id},
        execution_binding=record.input.execution_binding, agent_id=record.input.agent_id,
    )
    job = container.job_service._jobs[result.job_id]
    await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
    assert job.status == JobStatus.completed, job.error_message
    new_token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    assert new_token != old_token and len(case.compiled) == 2
    assert store.release_runtime_binding(sid, tid, old_token, messages=container.checkpointer) == ()
    assert store._catalog.runtime_bindings() == ((sid, tid, new_token),)
    assert store.read(sid, ref.file_id).data == b"runtime original"
    await case.tracker.close(session_id=sid)
    assert store._catalog.get_directory(aid).gc_after == (clock() + timedelta(days=30)).isoformat()


async def test_pin_to_queue_handoff_cannot_leave_a_false_unreferenced_window(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    message = await container.message_service.create(sid, MessageCreateRequest(content="下一条上传", attachments=[upload(b"next upload")]))
    original = SessionControlStore.list_non_terminal_leases
    accepted = False

    def accept_at_pin_read(control: SessionControlStore):
        nonlocal accepted
        if not accepted:
            accepted = True
            # 固定真实跨库交接时序：队列提交且 pin 已结算，然后才返回扫描读数。
            accept_queued_message(container.message_service, message)
        return original(control)

    monkeypatch.setattr(SessionControlStore, "list_non_terminal_leases", accept_at_pin_read)
    assert store.release_runtime_binding(sid, tid, token, messages=container.checkpointer) == ()
    assert accepted and container.checkpointer.execution_queue_snapshot(sid, tid).records
    assert store._catalog.get_directory(aid).gc_after is None
    assert (sid, tid, token) in store._catalog.runtime_bindings()


async def test_uri_resubmission_cannot_revive_a_directory_after_gc_wins(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.infrastructure.attachment_blob_catalog import (
        store as store_module,
    )

    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, _tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    await case.tracker.close(session_id=sid)
    clock.advance(30)
    removing = asyncio.Event()
    allow_remove = threading.Event()
    loop = asyncio.get_running_loop()
    original = store_module.remove_directory

    def pause_removal(path):
        loop.call_soon_threadsafe(removing.set)
        if not allow_remove.wait(10):
            raise TimeoutError("测试没有释放真实 GC 删除屏障")
        original(path)

    monkeypatch.setattr(store_module, "remove_directory", pause_removal)
    gc = asyncio.create_task(store.collect_garbage(leases=container.external_resource_leases))
    retry = None
    try:
        await asyncio.wait_for(removing.wait(), timeout=10)
        assert store._catalog.get_directory(aid).availability == "deleting"
        retry = asyncio.create_task(container.message_service.create(sid, MessageCreateRequest(content="回收竞争", attachments=[ref])))
        await asyncio.sleep(0)
        assert not retry.done()
    finally:
        allow_remove.set()
        assert await gc == (aid,)
    assert retry is not None
    with pytest.raises(FileNotFoundError):
        await retry
    assert store._catalog.get_directory(aid).availability == "tombstoned"
    with closing(store._open_control_store(sid)) as control:
        assert not control.list_non_terminal_leases()
