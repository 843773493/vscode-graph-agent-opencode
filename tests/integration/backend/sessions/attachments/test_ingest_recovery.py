"""真实上传在跨库 durable 边界硬退出后，恢复只结算同一 operation。"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.resource_uri import parse_resource_uri
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog import (
    directories as directories_module,
)
from app.services.infrastructure.attachment_blob_catalog import (
    store as store_module,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.message_service import accept_queued_message
from tests.support.workspaces import prepare_test_workspace

SESSION = "ses_019b781159d47374805bc4427d081a14"
BODY = b"durable-upload-crash-boundary"

_UPLOAD_WORKER = r'''
import asyncio
import base64
import os
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.core.path_utils import build_session_catalog_components, catalog_provider_scope
from app.core.phase_observer import bind_phase_observer
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.message_service import accept_queued_message

root, session, boundary = sys.argv[1:4]
phases = {"pin": "attachment.ingest.pin_acquired", "preparing": "attachment.ingest.preparing",
          "durable": "attachment.ingest.directory_durable", "published-active": "attachment.ingest.published"}
class Observer:
    async def reached(self, operation_id, phase, *, details=None):
        if phases.get(boundary) == phase:
            os._exit(73)
        if boundary == "concurrent-winner" and phase == "attachment.ingest.directory_durable":
            print("durable", flush=True)
            if sys.stdin.readline().strip() == "crash":
                os._exit(73)
    def reached_sync(self, operation_id, phase, *, details=None):
        pass
bind_phase_observer(Observer())
if boundary in {"staging", "rename"}:
    rename = os.rename
    def stop(source, target):
        if boundary == "staging":
            os._exit(73)
        rename(source, target)
        os._exit(73)
    os.rename = stop
if boundary.startswith("accepted"):
    original = SessionControlStore.mark_lease_settling
    def stop(control, **kwargs):
        if boundary == "accepted-settling":
            original(control, **kwargs)
        os._exit(73)
    SessionControlStore.mark_lease_settling = stop
components = build_session_catalog_components(Path(root) / ".boxteam/sessions", read_workspace_id(Path(root)))
options = {"clock": lambda: datetime.fromisoformat(sys.argv[4])} if len(sys.argv) == 5 else {}
with (
    catalog_provider_scope(components),
    closing(AttachmentBlobStore(Path(root), path_resolver=components.resolver, **options)) as store,
    RolloutCheckpointSaver(Path(root) / ".boxteam/sessions", attachment_store=store) as saver,
):
    messages = MessageService(main_thread_resolver=components.resolver.main_thread_id,
        checkpointer=saver, attachment_store=store, workspace_root=Path(root))
    body = base64.b64encode(b"durable-upload-crash-boundary").decode()
    attachment = AttachmentRef(file_id="inline:crash", name="crash.txt", content_type="text/plain",
                               data_url="data:text/plain;base64," + body)
    async def send():
        message = await messages.create(session, MessageCreateRequest(content="使用附件", attachments=[attachment]))
        accept_queued_message(messages, message)
    asyncio.run(send())
    if boundary.startswith("concurrent-"):
        print("accepted", flush=True)
        sys.exit(0)
    raise AssertionError("上传没有经过要求的崩溃边界")
'''


@dataclass
class IngestCase:
    root: Path
    store: AttachmentBlobStore
    saver: RolloutCheckpointSaver
    messages: MessageService

    def crash(self, boundary: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _UPLOAD_WORKER, str(self.root), SESSION, boundary],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 73, result.stdout + result.stderr

    def control(self) -> SessionControlStore:
        return SessionControlStore(self.store._path_resolver.resolve_session_node(SESSION) / "session-control.sqlite")


@pytest.fixture
def ingest_case(request: pytest.FixtureRequest, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = prepare_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces", template_items=())
    session_bundle_factory(root / ".boxteam/sessions", SESSION)
    with closing(AttachmentBlobStore(root)) as store, RolloutCheckpointSaver(root / ".boxteam/sessions", attachment_store=store) as saver:
        messages = MessageService(main_thread_resolver=store._path_resolver.main_thread_id,
                                  checkpointer=saver, attachment_store=store, workspace_root=root)
        yield IngestCase(root, store, saver, messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pin", "preparing", "staging", "rename", "durable", "published-active", "accepted-active", "accepted-settling"])
async def test_restart_recovers_upload_pin_at_real_durable_boundary(ingest_case, boundary):
    case = ingest_case
    case.crash(boundary)
    with closing(case.control()) as control:
        (before,) = control.list_non_terminal_leases()
    records = tuple(dict(row) for row in case.store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests"))
    accepted = boundary.startswith("accepted")
    published = boundary.startswith("published") or accepted
    assert bool(tuple(case.store._catalog.connection.execute("SELECT 1 FROM attachment_directories"))) == published
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    assert await case.store.recover_pending_ingests(messages=case.saver) == 0
    with closing(case.control()) as control:
        after = control.get_lease(before.lease_id)
        assert control.list_non_terminal_leases() == ()
    assert (after.operation_identity, after.preimage_hash, after.fencing_token) == (
        before.operation_identity, before.preimage_hash, before.fencing_token + 1,
    )
    assert after.state == ("completed" if accepted else "failed")
    assert case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION) == ()
    for record in records:
        assert not resolve_staging_path(case.store._attachments_root, record["staging_relative_locator"]).exists()
        directory = case.store._catalog.get_directory(record["attachment_id"])
        if published:
            assert directory.availability == "available"
            assert case.store.read(SESSION, case.store.reference(directory.attachment_id)).data == BODY
            assert bool(directory.gc_after) is not accepted
        else:
            assert directory is None
            assert not resolve_attachment_directory(case.store._attachments_root, record["relative_locator"]).exists()
    with closing(case.control()) as control, pytest.raises(RuntimeError, match="token 已失效"):
        case.store._settle_pin(control, before, outcome="completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pin", "published-active", "accepted-active"])
async def test_recovery_cannot_take_over_live_upload_holder(ingest_case, boundary):
    case = ingest_case
    case.crash(boundary)
    with closing(case.control()) as control:
        (before,) = control.list_non_terminal_leases()
    async with case.store.deletion_holder.lock_for(SESSION, exclusive=False):
        assert await asyncio.wait_for(case.store.recover_pending_ingests(messages=case.saver), timeout=1) == 0
        with closing(case.control()) as control:
            assert control.get_lease(before.lease_id) == before
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1


@pytest.mark.asyncio
async def test_thread_delete_waits_for_real_attachment_publication(ingest_case, monkeypatch):
    """删除必须先排空上传，避免先删 rollout 再留下晚到的 active owner-ref。"""
    case = ingest_case
    thread = case.store._path_resolver.main_thread_id(SESSION)
    publishing = asyncio.Event()
    release = asyncio.Event()
    observe = store_module.observe_phase

    async def paused_publish(operation_id, phase, *, details=None):
        if phase == "attachment.ingest.directory_durable":
            publishing.set()
            await release.wait()
        await observe(operation_id, phase, details=details)

    monkeypatch.setattr(store_module, "observe_phase", paused_publish)
    attachment = AttachmentRef(
        file_id="inline:delete-race", name="race.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )
    with RolloutCheckpointSaver(case.root / ".boxteam/sessions", attachment_store=case.store) as saver:
        saver._storage.storage.initialize(SESSION, thread_id=thread)

        async def delete_thread():
            owner = MutationIntentOwner(session_id=SESSION, thread_id=thread)
            with session_thread_owner_scope(owner, langgraph_thread_id=thread):
                await saver.adelete_thread(thread)

        upload = asyncio.create_task(case.store.persist_inline(SESSION, [attachment], thread_id=thread, message_id="msg_" + create_uuid_hex(namespace="message")))
        deletion = None
        try:
            await asyncio.wait_for(publishing.wait(), timeout=2)
            deletion = asyncio.create_task(delete_thread())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(deletion), timeout=0.15)
        finally:
            release.set()
            await asyncio.gather(upload, *([deletion] if deletion is not None else []))
        assert case.store._catalog.connection.execute("SELECT 1 FROM attachment_directory_associations WHERE state='active'").fetchall() == []
        assert not saver._storage.storage.root(SESSION, thread_id=thread).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["mark_lease_settling", "settle_lease"])
async def test_settlement_error_preserves_durable_published_subject(ingest_case, monkeypatch, method):
    case = ingest_case
    original = getattr(SessionControlStore, method)
    failed = False

    def fail_once(control, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected attachment settlement failure")
        return original(control, **kwargs)

    monkeypatch.setattr(SessionControlStore, method, fail_once)
    attachment = AttachmentRef(
        file_id="inline:settle", name="settle.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )
    message = await case.messages.create(SESSION, MessageCreateRequest(content="使用附件", attachments=[attachment]))
    with pytest.raises(RuntimeError, match="injected attachment settlement failure"):
        queued_identity = UserExecutionIdentity(
            job_id="job_settlement", turn_id="turn_settlement", admission_idempotency_key="adm_settlement",
        )
        case.messages.accept_user_message(message, execution_identity=queued_identity, queue_input=ExecutionQueueInput(
            execution_binding=queued_identity, message_id=message.message_id, agent_id="default",
            provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
            created_at=message.created_at.isoformat(),
        ))
    (ref,) = tuple(case.store._catalog.connection.execute("SELECT attachment_id FROM attachment_directories"))
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    assert pin.state == ("active" if method == "mark_lease_settling" else "settling")
    assert case.store._catalog.get_ingest_record(pin.operation_identity).state == "published"
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id).state == "completed"
    assert case.store.read(SESSION, case.store.reference(ref[0])).data == BODY


@pytest.fixture
def attachment_message_request():
    return MessageCreateRequest(content="使用附件", attachments=[AttachmentRef(
        file_id="inline:message", name="message.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
    )])


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True], ids=["user", "internal"])
@pytest.mark.parametrize("same_body", [False, True], ids=["different-body", "same-body"])
async def test_recovery_releases_only_unaccepted_message_reference(
    ingest_case, attachment_message_request, internal, same_body,
):
    """真实接受一条消息后遗弃下一条，不能释放同 Thread 的已接受使用。"""
    case = ingest_case
    request = attachment_message_request
    if internal:
        prepared = internal_message_factory.build(
            kind="session_message", control="处理消息附件。",
            sections=(PromptSection("control_context", {"source_session_id": SESSION}),
                      PromptSection("session_message", "内部控制正文")),
        )
        request = request.model_copy(update={"content": prepared.content, "metadata": prepared.metadata})
    message = await case.messages.create(SESSION, request)
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    assert pin.recovery_ref == f"attachment:{SESSION}:{message.thread_id}:{message.message_id}"
    if internal:
        queued_input = SimpleNamespace(
            session_id=SESSION, thread_id=message.thread_id, message_id=message.message_id,
            message=message.content, message_metadata=message.metadata, attachments=message.attachments,
            message_created_at=message.created_at.isoformat(), job_id=create_prefixed_id("job"),
            execution_binding=InternalExecutionIntent(message_id=message.message_id,
                execution_id=create_prefixed_id("intr"), admission_idempotency_key=create_prefixed_id("adm")),
        )
        case.messages.accept_job_input(queued_input, queue_input=ExecutionQueueInput(execution_binding=queued_input.execution_binding, message_id=queued_input.message_id, agent_id="default", provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=queued_input.message_created_at))
    else:
        queued_identity = UserExecutionIdentity(
            job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"), admission_idempotency_key=create_prefixed_id("adm"),
        )
        case.messages.accept_user_message(message, execution_identity=queued_identity, queue_input=ExecutionQueueInput(
            execution_binding=queued_identity, message_id=message.message_id, agent_id="default",
            provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
            created_at=message.created_at.isoformat(),
        ))
    with closing(case.control()) as control:
        completed = control.get_lease(pin.lease_id)
    assert completed.state == "completed"
    abandoned_request = attachment_message_request
    if not same_body:
        abandoned_request = abandoned_request.model_copy(update={"attachments": [AttachmentRef(
            file_id="inline:orphan", name="orphan.txt", content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode(b"unaccepted-only").decode(),
        )]})
    abandoned = await case.messages.create(SESSION, abandoned_request)
    assert abandoned.message_id != message.message_id
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id) == completed
        assert control.list_non_terminal_leases() == ()
        failed = control.connection.execute("SELECT state FROM session_operation_leases WHERE recovery_ref=?", (
            f"attachment:{SESSION}:{abandoned.thread_id}:{abandoned.message_id}",
        )).fetchone()
        assert failed[0] == "failed"
    assert abandoned.attachments[0].file_id != message.attachments[0].file_id
    current = datetime.now(UTC)
    case.store._clock = lambda: current + timedelta(days=31)
    await case.store.collect_garbage(leases=ExternalResourceLeaseLedger(state_path=case.root / ".boxteam/resources.json"))
    assert case.store.read(SESSION, message.attachments[0].file_id).data == BODY
    with pytest.raises(FileNotFoundError):
        case.store.read(SESSION, abandoned.attachments[0].file_id)


@pytest.mark.asyncio
async def test_recovery_rejects_pin_without_frozen_message_identity(ingest_case):
    case = ingest_case
    case.crash("pin")
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
        legacy = pin.recovery_ref.rsplit(":", 1)[0]
        control.connection.execute("UPDATE session_operation_leases SET recovery_ref=? WHERE lease_id=?", (legacy, pin.lease_id))
        control.connection.commit()
        before = control.get_lease(pin.lease_id)
    with pytest.raises(RuntimeError, match="缺少精确消息身份"):
        await case.store.recover_pending_ingests(messages=case.saver)
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id) == before


@pytest.mark.asyncio
async def test_recovery_rejects_canonical_reference_index_drift(ingest_case):
    case = ingest_case
    case.crash("accepted-active")
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    thread_id, message_id = pin.recovery_ref.split(":")[2:]
    storage = case.saver._storage.storage
    with closing(sqlite3.connect(storage.index_path(SESSION, thread_id=thread_id))) as connection:
        connection.execute("DELETE FROM item_resource_references WHERE item_id=?", (f"item-{message_id}",))
        connection.commit()
    refs = tuple(case.store._catalog.connection.execute("SELECT * FROM attachment_directory_associations ORDER BY attachment_id"))
    with pytest.raises(RuntimeError, match="附件正文与使用索引不一致"):
        await case.store.recover_pending_ingests(messages=case.saver)
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id) == pin
    assert tuple(case.store._catalog.connection.execute("SELECT * FROM attachment_directory_associations ORDER BY attachment_id")) == refs


@pytest.fixture
async def upload_process_factory(ingest_case):
    """进程只从真实 MessageService 入口创建并接受附件消息，fixture 负责排空。"""
    processes = []

    async def start(session_id, boundary, *, claim_time=None):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _UPLOAD_WORKER, str(ingest_case.root), session_id, boundary,
            *([claim_time] if claim_time is not None else []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_winner", [False, True])
async def test_concurrent_processes_publish_independent_directories_across_utc_dates(
    ingest_case, upload_process_factory, session_bundle_factory, crash_winner,
):
    case = ingest_case
    other = "ses_019b781159d47374805bc4427d081a15"
    session_bundle_factory(case.root / ".boxteam/sessions", other)
    first = await upload_process_factory(SESSION, "concurrent-winner", claim_time="2026-10-23T23:59:59+00:00")
    assert await asyncio.wait_for(first.stdout.readline(), timeout=20) == b"durable\n"
    second = await upload_process_factory(other, "concurrent-contender", claim_time="2026-10-24T00:00:01+00:00")
    stdout, stderr = await asyncio.wait_for(second.communicate(), timeout=20)
    assert second.returncode == 0 and stdout == b"accepted\n", stdout + stderr
    assert first.returncode is None
    first.stdin.write(b"crash\n" if crash_winner else b"publish\n")
    await first.stdin.drain()
    stdout, stderr = await asyncio.wait_for(first.communicate(), timeout=20)
    assert first.returncode == (73 if crash_winner else 0), stdout + stderr
    records = tuple(dict(row) for row in case.store._catalog.connection.execute("SELECT * FROM attachment_directory_ingests ORDER BY created_at"))
    assert len(records) == 2 and records[0]["attachment_id"] != records[1]["attachment_id"]
    assert records[0]["digest"] == records[1]["digest"]
    assert records[0]["relative_locator"].startswith("2026/10/23/")
    assert records[1]["relative_locator"].startswith("2026/10/24/")
    assert await case.store.recover_pending_ingests(messages=case.saver) == int(crash_winner)
    with closing(AttachmentBlobStore(case.root)) as cold:
        assert cold.read(other, cold.reference(records[1]["attachment_id"])).data == BODY
        if crash_winner:
            assert cold._catalog.get_directory(records[0]["attachment_id"]) is None
            assert not resolve_attachment_directory(cold._attachments_root, records[0]["relative_locator"]).exists()
        else:
            assert cold.read(SESSION, cold.reference(records[0]["attachment_id"])).data == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preparing", "rename"])
async def test_unpublished_directory_recovery_preserves_old_intent_and_new_upload_is_independent(
    ingest_case, attachment_message_request, boundary,
):
    case = ingest_case
    case.crash(boundary)
    (record,) = case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION)
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    before = case.store._catalog.get_ingest_record(record.ingest_idempotency_key)
    assert before.state == "aborted"
    message = await case.messages.create(SESSION, attachment_message_request)
    accept_queued_message(case.messages, message)
    assert case.store._catalog.get_ingest_record(record.ingest_idempotency_key) == before
    assert case.store.read(SESSION, message.attachments[0].file_id).data == BODY
    assert parse_resource_uri(message.attachments[0].file_id).attachment_id != record.attachment_id
    assert not resolve_attachment_directory(case.store._attachments_root, record.relative_locator).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["staging", "rename"])
async def test_aborted_directory_recovery_retries_physical_cleanup_before_settling_pin(
    ingest_case, monkeypatch, boundary,
):
    case = ingest_case
    case.crash(boundary)
    (record,) = case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION)
    path = (resolve_staging_path(case.store._attachments_root, record.staging_relative_locator)
            if boundary == "staging" else resolve_attachment_directory(case.store._attachments_root, record.relative_locator))
    assert path.is_dir()
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    remove = directories_module.remove_directory
    def interrupted(target):
        if target == path:
            raise OSError("目录清理中断")
        return remove(target)
    with monkeypatch.context() as patch:
        patch.setattr(directories_module, "remove_directory", interrupted)
        with pytest.raises(OSError, match="目录清理中断"):
            await case.store.recover_pending_ingests(messages=case.saver)
    assert case.store._catalog.get_ingest_record(record.ingest_idempotency_key).state == "aborted"
    assert path.is_dir()
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id) == pin
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    assert not path.exists()
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id).state == "failed"
    assert await case.store.recover_pending_ingests(messages=case.saver) == 0
