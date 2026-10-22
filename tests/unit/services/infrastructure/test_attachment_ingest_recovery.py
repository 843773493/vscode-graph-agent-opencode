"""真实上传在跨库 durable 边界硬退出后，恢复只结算同一 operation。"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION = "ses_019b781159d47374805bc4427d081a14"
BODY = b"durable-upload-crash-boundary"

_UPLOAD_WORKER = r'''
import asyncio
import base64
import os
import sys
from pathlib import Path

from app.core.path_utils import build_session_catalog_components, catalog_provider_scope
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver

root, session, boundary = sys.argv[1:]
components = build_session_catalog_components(Path(root) / ".boxteam" / "sessions", read_workspace_id(Path(root)))
with catalog_provider_scope(components):
    store = AttachmentBlobStore(Path(root), path_resolver=components.resolver)
    saver = RolloutCheckpointSaver(Path(root) / ".boxteam/sessions", attachment_store=store)
    messages = MessageService(main_thread_resolver=components.resolver.main_thread_id,
        checkpointer=saver, attachment_store=store, workspace_root=Path(root))
    thread = store._path_resolver.main_thread_id(session)
    if boundary == "pin":
        store._catalog.create_or_get_ingest_record = lambda **kwargs: os._exit(73)
    elif boundary in {"staging", "rename"}:
        name = "_write_staging" if boundary == "staging" else "_commit_staged_blob"
        original = getattr(store, name)
        def stop(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        setattr(store, name, stop)
    elif boundary == "published-active":
        original = store._publish
        async def stop(**kwargs):
            await original(**kwargs)
            os._exit(73)
        store._publish = stop
    else:
        original = SessionControlStore.mark_lease_settling
        def stop(control, **kwargs):
            if boundary == "accepted-settling":
                original(control, **kwargs)
            os._exit(73)
        SessionControlStore.mark_lease_settling = stop
    body = base64.b64encode(b"durable-upload-crash-boundary").decode()
    attachment = AttachmentRef(file_id="inline:crash", name="crash.txt", content_type="text/plain",
                               data_url="data:text/plain;base64," + body)
    async def send():
        message = await messages.create(session, MessageCreateRequest(content="使用附件", attachments=[attachment]), thread_id=thread)
        messages.accept_user_message(message, execution_identity=UserExecutionIdentity(
            job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"),
            admission_idempotency_key=create_prefixed_id("adm")))
    asyncio.run(send())
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
def ingest_case(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", SESSION)
    store = AttachmentBlobStore(tmp_path)
    with RolloutCheckpointSaver(tmp_path / ".boxteam/sessions", attachment_store=store) as saver:
        messages = MessageService(main_thread_resolver=store._path_resolver.main_thread_id,
                                  checkpointer=saver, attachment_store=store, workspace_root=tmp_path)
        yield IngestCase(tmp_path, store, saver, messages)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pin", "staging", "rename", "published-active", "accepted-active", "accepted-settling"])
async def test_restart_recovers_upload_pin_at_real_durable_boundary(ingest_case, boundary):
    case = ingest_case
    case.crash(boundary)
    with closing(case.control()) as control:
        (before,) = control.list_non_terminal_leases()
    records = case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION)
    staging = [resolve_staging_path(case.store._attachments_root, row.staging_relative_locator) for row in records]
    accepted = boundary.startswith("accepted")
    published = boundary.startswith("published") or accepted
    refs = case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True)
    assert bool(refs) == published

    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    assert await case.store.recover_pending_ingests(messages=case.saver) == 0
    with closing(case.control()) as control:
        after = control.get_lease(before.lease_id)
        assert control.list_non_terminal_leases() == ()
    assert after.operation_identity == before.operation_identity
    assert after.preimage_hash == before.preimage_hash
    assert after.fencing_token == before.fencing_token + 1
    assert after.state == ("completed" if accepted else "failed")
    assert case.store._catalog.list_non_terminal_ingest_records(owner_session_id=SESSION) == ()
    assert all(not path.exists() for path in staging)
    with closing(case.control()) as control, pytest.raises(RuntimeError, match="token 已失效"):
        case.store._settle_pin(control, before, outcome="completed")
    assert case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True) == (refs if accepted else ())
    if accepted:
        assert case.store.read(SESSION, case.store.reference(SESSION, refs[0].attachment_id)).data == BODY
    elif published:
        with pytest.raises(FileNotFoundError):
            case.store.read(SESSION, case.store.reference(SESSION, refs[0].attachment_id))


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
    publish = case.store._publish

    async def paused_publish(**kwargs):
        publishing.set()
        await release.wait()
        return await publish(**kwargs)

    monkeypatch.setattr(case.store, "_publish", paused_publish)
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
        assert case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True) == ()
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
        case.messages.accept_user_message(message, execution_identity=UserExecutionIdentity(
            job_id="job_settlement", turn_id="turn_settlement", admission_idempotency_key="adm_settlement",
        ))
    (ref,) = case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True)
    with closing(case.control()) as control:
        (pin,) = control.list_non_terminal_leases()
    assert pin.state == ("active" if method == "mark_lease_settling" else "settling")
    assert case.store._catalog.get_ingest_record(pin.operation_identity).state == "published"
    assert await case.store.recover_pending_ingests(messages=case.saver) == 1
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id).state == "completed"
    assert case.store.read(SESSION, case.store.reference(SESSION, ref.attachment_id)).data == BODY


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
        case.messages.accept_job_input(SimpleNamespace(
            session_id=SESSION, thread_id=message.thread_id, message_id=message.message_id,
            message=message.content, message_metadata=message.metadata, attachments=message.attachments,
            message_created_at=message.created_at.isoformat(), job_id=create_prefixed_id("job"),
            execution_binding=InternalExecutionIntent(message_id=message.message_id,
                execution_id=create_prefixed_id("intr"), admission_idempotency_key=create_prefixed_id("adm")),
        ))
    else:
        case.messages.accept_user_message(message, execution_identity=UserExecutionIdentity(
            job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"), admission_idempotency_key=create_prefixed_id("adm"),
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
    case.store.collect_garbage(retention=timedelta(0))
    assert case.store.read(SESSION, message.attachments[0].file_id).data == BODY
    if same_body:
        assert abandoned.attachments[0].file_id == message.attachments[0].file_id
    else:
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
    with pytest.raises(RuntimeError, match="缺少冻结的消息身份"):
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
    refs = case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True)
    with pytest.raises(RuntimeError, match="附件正文与使用索引不一致"):
        await case.store.recover_pending_ingests(messages=case.saver)
    with closing(case.control()) as control:
        assert control.get_lease(pin.lease_id) == pin
    assert case.store._catalog.list_owner_refs_for_session(SESSION, active_only=True) == refs
