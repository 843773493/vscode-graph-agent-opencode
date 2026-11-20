"""预上传 HTTP 入口、消息交接与无前端删除通知的回收。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import (
    get_attachment_blob_store,
    get_rollout_checkpoint_saver,
    verify_local_token,
)
from app.api.messages import router
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.services.infrastructure.attachment_blob_catalog import uploads
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_file_version,
)
from tests.integration.backend.sessions.attachments.test_ingest_recovery import (
    SESSION,
)
from tests.integration.backend.sessions.attachments.test_ingest_recovery import (
    ingest_case as ingest_case,  # noqa: PLC0414
)
from tests.support.message_service import accept_queued_message


@pytest.fixture
async def upload_case(ingest_case):
    case = ingest_case
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_attachment_blob_store] = lambda: case.store
    app.dependency_overrides[get_rollout_checkpoint_saver] = lambda: case.saver
    app.dependency_overrides[verify_local_token] = lambda: "test"
    thread = case.store._path_resolver.main_thread_id(SESSION)
    path = f"/api/v1/sessions/{SESSION}/threads/{thread}/attachments"
    now = [datetime(2026, 11, 20, tzinfo=UTC)]
    case.store._clock = lambda: now[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield case, client, path, thread, now


async def upload(client, path, content=b"unsent file") -> AttachmentRef:
    response = await client.post(path, params={"name": "说明.txt"}, content=content,
                                 headers={"Content-Type": "text/plain"})
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    ref = AttachmentRef.model_validate(response.json()["data"])
    assert ref.directory_uri and ref.data_url is None
    return ref


@pytest.mark.asyncio
async def test_upload_does_not_create_message_and_late_remove_cannot_delete_accepted_input(upload_case):
    case, client, path, thread, now = upload_case
    ref = await upload(client, path)
    assert (await case.messages.list(SESSION)).items == []
    assert (await case.store.list_thread_attachments(SESSION, thread)).items == []
    assert case.store.read(SESSION, ref.file_id).data == b"unsent file"
    assert await case.store.recover_pending_ingests(messages=case.saver) == 0

    message = await case.messages.create(SESSION, MessageCreateRequest(content="读取附件", attachments=[ref]), thread_id=thread)
    accept_queued_message(case.messages, message)
    with closing(case.control()) as control:
        assert control.list_non_terminal_leases() == ()
    response = await client.delete(path, params={"file_id": ref.file_id})
    assert response.status_code == 200, response.text
    now[0] += timedelta(days=2)
    assert await case.store.expire_draft_uploads(messages=case.saver) == 0
    assert case.store.read(SESSION, ref.file_id).data == b"unsent file"
    assert case.saver.attachment_message_references(SESSION, thread, message.message_id) == (ref.file_id,)
    assert (await case.store.list_thread_attachments(SESSION, thread)).items[0].file_id == ref.file_id


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["delete", "expiry", "restart"])
async def test_abandoned_upload_reclaims_directory_and_original_without_frontend_notification(upload_case, cleanup):
    case, client, path, _thread, now = upload_case
    ref = await upload(client, path)
    (record,) = case.store._catalog.connection.execute("SELECT digest,relative_locator FROM attachment_directory_ingests").fetchall()
    version = resolve_file_version(case.store._attachments_root, record[0])
    directory = case.store._attachments_root / record[1]
    assert version.is_file() and directory.is_dir()
    if cleanup == "delete":
        response = await client.delete(path, params={"file_id": ref.file_id})
        assert response.status_code == 200, response.text
        assert (await client.delete(path, params={"file_id": ref.file_id})).status_code == 200
    else:
        now[0] += timedelta(hours=25)
        recover = case.store.expire_draft_uploads if cleanup == "expiry" else case.store.recover_pending_ingests
        assert await recover(messages=case.saver) == 1
        assert await recover(messages=case.saver) == 0
    assert not directory.exists() and not version.exists()
    with pytest.raises(FileNotFoundError):
        case.store.read(SESSION, ref.file_id)
    assert (await case.messages.list(SESSION)).items == []


@pytest.mark.asyncio
async def test_expiring_identical_draft_keeps_accepted_original_version(upload_case):
    case, client, path, thread, now = upload_case
    accepted, abandoned = await upload(client, path), await upload(client, path)
    assert accepted.file_id != abandoned.file_id
    message = await case.messages.create(SESSION, MessageCreateRequest(content="保存", attachments=[accepted]), thread_id=thread)
    accept_queued_message(case.messages, message)
    now[0] += timedelta(hours=25)
    assert await case.store.expire_draft_uploads(messages=case.saver) == 1
    assert case.store.read(SESSION, accepted.file_id).data == b"unsent file"
    with pytest.raises(FileNotFoundError):
        case.store.read(SESSION, abandoned.file_id)


@pytest.mark.asyncio
async def test_interrupted_transfer_has_no_published_attachment_or_temporary_file(upload_case):
    case, _client, _path, thread, _now = upload_case

    async def interrupted():
        yield b"partial bytes"
        raise ConnectionError("浏览器关闭连接")

    with pytest.raises(ConnectionError, match="关闭连接"):
        await case.store.upload_attachment(SESSION, thread, name="partial.txt", content_type="text/plain", chunks=interrupted())
    assert tuple(case.store._catalog.connection.execute("SELECT 1 FROM attachment_directory_ingests")) == ()
    assert not tuple(case.store._attachments_root.glob("tmp*"))


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_before_publication(upload_case, monkeypatch):
    case, client, path, _thread, _now = upload_case
    monkeypatch.setattr(uploads, "MAX_ATTACHMENT_BYTES", 8)
    response = await client.post(path, params={"name": "large.bin"}, content=b"123456789")
    assert response.status_code == 400
    assert tuple(case.store._catalog.connection.execute("SELECT 1 FROM attachment_directory_ingests")) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["partial", "published"])
async def test_hard_process_exit_does_not_require_frontend_delete(upload_case, boundary):
    case, _client, _path, _thread, now = upload_case
    worker = r'''
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from app.core.path_utils import build_session_catalog_components, catalog_provider_scope
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger
root, session, boundary, timestamp = Path(sys.argv[1]), *sys.argv[2:]
components = build_session_catalog_components(root / ".boxteam/sessions", read_workspace_id(root))
with catalog_provider_scope(components):
    store = AttachmentBlobStore(root, path_resolver=components.resolver,
        leases=ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.sqlite"),
        clock=lambda: datetime.fromisoformat(timestamp))
    async def chunks():
        yield b"hard-exit-upload"
        if boundary == "partial":
            os._exit(73)
    asyncio.run(store.upload_attachment(session, components.resolver.main_thread_id(session),
        name="crash.txt", content_type="text/plain", chunks=chunks()))
    os._exit(73)
'''
    process = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", worker, str(case.root), SESSION, boundary, now[0].isoformat()],
                             capture_output=True, text=True, timeout=20, check=False)
    assert process.returncode == 73, process.stdout + process.stderr
    assert not tuple(case.store._attachments_root.glob("tmp*"))
    now[0] += timedelta(hours=25)
    assert await case.store.recover_pending_ingests(messages=case.saver) == (1 if boundary == "published" else 0)
    assert not tuple((case.store._attachments_root / ".versions/sha256").glob("*"))


@pytest.mark.asyncio
async def test_failed_message_handoff_does_not_leave_draft_reference_protection(upload_case):
    case, client, path, thread, _now = upload_case
    ref = await upload(client, path)
    # 消息身份已生成且引用取得 pin，但尚未经过 canonical 接受。
    await case.messages.create(SESSION, MessageCreateRequest(content="未能准入", attachments=[ref]), thread_id=thread)
    response = await client.delete(path, params={"file_id": ref.file_id})
    assert response.status_code == 200, response.text
    with closing(case.control()) as control:
        assert control.list_non_terminal_leases() == ()
    with pytest.raises(FileNotFoundError):
        case.store.read(SESSION, ref.file_id)
    assert not tuple((case.store._attachments_root / ".versions/sha256").glob("*"))
