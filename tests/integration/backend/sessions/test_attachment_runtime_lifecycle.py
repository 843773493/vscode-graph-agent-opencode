"""从真实消息和图执行生成目录关联，验证卸载、冷 owner 与排队输入。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.resource_uri import file_uri_child, parse_attachment_uri
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.job import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.text_mutation import CreateText, PatchText
from app.services.business.job.service import JobService
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
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
from tests.support.session_navigation.catalog_intents import (
    create_catalog_intent,
    submit_catalog_intent,
)


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
    assert isinstance(container.job_service, JobService)
    job = container.job_service._jobs[result.job_id]
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
    assert job.status == JobStatus.completed, job.error_message
    ref, = job.attachments
    return session.session_id, result.thread_id, ref


def isolate_unused_external_managers(case: RuntimeCase, monkeypatch: pytest.MonkeyPatch) -> None:
    for owner in (case.runner.container.terminal_resource_owner, case.runner.container.browser_resource_owner):
        monkeypatch.setattr(owner._transport, "list_resources", AsyncMock(return_value=[]))


def attachment_directory(store: AttachmentBlobStore, attachment_id: str) -> Path:
    record = store._catalog.get_directory(attachment_id)
    assert record is not None
    return resolve_attachment_directory(store._attachments_root, record.relative_locator)


async def test_real_graph_unload_releases_unmodified_copy_and_historical_uri_rebuilds_original(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    store = case.runner.container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert aid is not None
    assert len(case.compiled) == 1
    generation = case.tracker.snapshot(sid, tid).generation
    token = case.tracker.runtime_binding_token(generation)
    directory = attachment_directory(store, aid)
    identity = ref.model_dump(mode="json")
    assert (directory / "original/content").read_bytes() == b"runtime original"
    assert store._catalog.runtime_bindings() == ((sid, tid, token),)
    assert not case.runner.saver.execution_queue_snapshot(sid, tid).records
    await case.tracker.close(session_id=sid)
    assert store._catalog.runtime_bindings() == ()
    assert not directory.exists()
    assert case.runner.saver.attachment_reference_in_use(sid, tid, aid)
    assert store.read(sid, ref.file_id).data == b"runtime original"
    assert store.describe_reference(ref, session_id=sid).model_dump(mode="json") == identity
    assert attachment_directory(store, aid) == directory
    assert store._catalog.runtime_bindings() == (), "历史查看不产生永久运行时关联"


async def test_cold_owner_releases_crash_binding_and_keeps_rebuildable_identity(
    runtime_case: RuntimeCase, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    store = case.runner.container.attachment_blob_store
    monkeypatch.setattr(store, "_clock", clock)
    isolate_unused_external_managers(case, monkeypatch)
    sid, _tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert aid is not None
    directory = attachment_directory(store, aid)
    cold_tracker = ThreadResidencyTracker()
    with closing(AttachmentBlobStore(case.runner.workspace, path_resolver=case.runner.container.session_service.path_resolver,
                                    leases=case.runner.container.external_resource_leases, clock=clock)) as cold:
        assert await cold.reconcile_runtime_bindings(messages=case.runner.saver, is_current=cold_tracker.is_current_runtime_binding) == (aid,)
        assert not directory.exists()
        clock.advance(12)
        assert await cold.reconcile_runtime_bindings(messages=case.runner.saver, is_current=cold_tracker.is_current_runtime_binding) == ()
        assert cold.read(sid, ref.file_id).data == b"runtime original"
        assert attachment_directory(cold, aid) == directory


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
    assert aid is not None
    directory = attachment_directory(store, aid)
    await case.tracker.close(session_id=sid)
    assert not directory.exists()
    message = await container.message_service.create(sid, MessageCreateRequest(content="再次使用 URI", attachments=[ref]))
    with closing(store._open_control_store(sid)) as control:
        pin, = control.list_non_terminal_leases()
        assert pin.operation_identity.startswith("attref_") and pin.state == "active"
    assert directory.is_dir(), "新消息的引用准入重建原工作目录"
    if accepted:
        accept_queued_message(container.message_service, message)
    cold_tracker = ThreadResidencyTracker()
    with closing(AttachmentBlobStore(case.runner.workspace, path_resolver=container.session_service.path_resolver,
                                    leases=container.external_resource_leases, clock=clock)) as cold:
        await cold.recover_idle_session(sid, messages=container.checkpointer)
        assert await cold.reconcile_runtime_bindings(messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding) == ()
        with closing(cold._open_control_store(sid)) as control:
            assert not control.list_non_terminal_leases()
        assert directory.exists() is accepted
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
    assert await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=tracker.is_current_runtime_binding) == ()
    assert store.read(session.session_id, message.attachments[0].file_id).data == b"cold queued"
    aid = parse_attachment_uri(message.attachments[0].file_id, require_file=True).attachment_id
    assert aid is not None
    assert attachment_directory(store, aid).is_dir()


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
    assert aid is not None
    old_token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    queued = await container.message_service.create(sid, MessageCreateRequest(content="使用此前的附件"))
    accept_queued_message(container.message_service, queued)
    record, = container.checkpointer.execution_queue_snapshot(sid, tid).records
    await case.tracker.close(session_id=sid)
    assert store._catalog.runtime_bindings() == ((sid, tid, old_token),)
    cold_tracker = ThreadResidencyTracker()
    assert await store.reconcile_runtime_bindings(messages=container.checkpointer, is_current=cold_tracker.is_current_runtime_binding) == ()
    directory = attachment_directory(store, aid)
    assert directory.is_dir()
    result = await container.job_service.start_job(
        sid, queued.content, thread_id=tid, message_id=queued.message_id,
        message_created_at=queued.created_at.isoformat(),
        message_metadata={"boxteam_session_provider_id": record.input.provider_id},
        execution_binding=record.input.execution_binding, agent_id=record.input.agent_id,
    )
    assert isinstance(container.job_service, JobService)
    job = container.job_service._jobs[result.job_id]
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
    assert job.status == JobStatus.completed, job.error_message
    new_token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    assert new_token != old_token and len(case.compiled) == 2
    assert await store.release_runtime_binding(sid, tid, old_token, messages=container.checkpointer) == ()
    assert store._catalog.runtime_bindings() == ((sid, tid, new_token),)
    assert store.read(sid, ref.file_id).data == b"runtime original"
    await case.tracker.close(session_id=sid)
    assert not directory.exists()


async def test_pin_to_queue_handoff_cannot_leave_a_false_unreferenced_window(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert aid is not None
    token = case.tracker.runtime_binding_token(case.tracker.snapshot(sid, tid).generation)
    message = await container.message_service.create(sid, MessageCreateRequest(content="下一条上传", attachments=[upload(b"next upload")]))
    original = SessionControlStore.list_non_terminal_leases
    accepted = False
    loop = asyncio.get_running_loop()

    async def accept_on_event_loop() -> None:
        accept_queued_message(container.message_service, message)

    def accept_at_pin_read(control: SessionControlStore):
        nonlocal accepted
        if not accepted:
            accepted = True
            # 固定真实跨库交接时序：队列提交且 pin 已结算，然后才返回扫描读数。
            asyncio.run_coroutine_threadsafe(accept_on_event_loop(), loop).result(timeout=5)
        return original(control)

    monkeypatch.setattr(SessionControlStore, "list_non_terminal_leases", accept_at_pin_read)
    assert await store.release_runtime_binding(sid, tid, token, messages=container.checkpointer) == ()
    assert accepted and container.checkpointer.execution_queue_snapshot(sid, tid).records
    assert attachment_directory(store, aid).is_dir()
    assert (sid, tid, token) in store._catalog.runtime_bindings()


async def test_uri_resubmission_waits_for_release_and_rebuilds_the_same_directory(
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
    assert aid is not None
    directory = attachment_directory(store, aid)
    removing = asyncio.Event()
    allow_remove = threading.Event()
    loop = asyncio.get_running_loop()
    original = store_module.remove_directory

    def pause_removal(path):
        loop.call_soon_threadsafe(removing.set)
        if not allow_remove.wait(10):
            raise TimeoutError("测试没有释放工作副本删除屏障")
        original(path)

    monkeypatch.setattr(store_module, "remove_directory", pause_removal)
    release = asyncio.create_task(case.tracker.close(session_id=sid))
    retry = None
    try:
        await asyncio.wait_for(removing.wait(), timeout=10)
        retry = asyncio.create_task(container.message_service.create(sid, MessageCreateRequest(content="回收竞争", attachments=[ref])))
        await asyncio.sleep(0)
        assert not retry.done()
    finally:
        allow_remove.set()
        await release
    assert retry is not None
    message = await retry
    restored, = message.attachments
    assert (restored.file_id, restored.directory_uri) == (ref.file_id, ref.directory_uri)
    assert attachment_directory(store, aid) == directory
    assert store.read(sid, restored.file_id).data == b"runtime original"
    accept_queued_message(container.message_service, message)
    with closing(store._open_control_store(sid)) as control:
        assert not control.list_non_terminal_leases()


@pytest.mark.parametrize("remove_result", [False, True], ids=["session-delete-keeps-results", "back-to-baseline"])
async def test_file_tool_results_survive_repeated_unload_and_cold_owner_until_explicitly_removed(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch, remove_result: bool,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    container = case.runner.container
    store = container.attachment_blob_store
    sid, _tid, ref = await execute_upload(case)
    assert ref.directory_uri is not None
    aid = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert aid is not None
    directory = attachment_directory(store, aid)
    output = file_uri_child(ref.directory_uri, "derived", "处理结果.txt")
    created = await container.text_mutations.execute_async(CreateText(
        target=output, content="必须保留的处理结果 Ω\n", expected_version="absent",
    ))
    assert created["status"] == "success"
    for _ in range(2):
        await case.tracker.close(session_id=sid)
        assert (directory / "derived/处理结果.txt").read_text() == "必须保留的处理结果 Ω\n"
        with closing(AttachmentBlobStore(case.runner.workspace, path_resolver=container.session_service.path_resolver)) as cold:
            assert cold.read(sid, output).data == "必须保留的处理结果 Ω\n".encode()
            assert attachment_directory(cold, aid) == directory
        result = await container.session_orchestrator.create_message(sid, MessageRunRequest(
            message=MessageCreateRequest(content="继续使用已有处理结果", attachments=[ref]), run=RunOptions(),
        ))
        assert isinstance(container.job_service, JobService)
        job = container.job_service._jobs[result.job_id]
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), timeout=15)
        assert job.status == JobStatus.completed, job.error_message
    if remove_result:
        current = await container.workspace_service.get_file_content(path=output)
        removed = await container.text_mutations.execute_async(PatchText(
            target=ref.directory_uri,
            input="*** Begin Patch\n*** Delete File: derived/处理结果.txt\n*** End Patch",
            expected_versions={"derived/处理结果.txt": "sha256:" + current.revision},
            explanation="显式删除处理结果，恢复附件初始基线",
        ))
        assert removed["status"] == "success"
        await case.tracker.close(session_id=sid)
        assert not directory.exists()
        assert store.read(sid, ref.file_id).data == b"runtime original"
        with pytest.raises(FileNotFoundError):
            store.read(sid, output)
    else:
        catalog = container.session_catalog_service
        receipt = await submit_catalog_intent(catalog, create_catalog_intent(
            catalog, "delete_session", target_node_id=sid,
        ))
        assert receipt.state == "committed" and not receipt.pending_settlement
        assert (directory / "derived/处理结果.txt").read_text() == "必须保留的处理结果 Ω\n"
        assert store.read(None, output).data == "必须保留的处理结果 Ω\n".encode()
