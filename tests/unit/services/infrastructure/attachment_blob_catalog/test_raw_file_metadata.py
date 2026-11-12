"""原 raw HEAD/GET 共享附件 catalog 的名称、MIME 和文件准入。"""

from __future__ import annotations

import base64
import errno
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import anyio
import httpx
import pytest
from fastapi import FastAPI

from app.api import workspace
from app.api.deps import get_workspace_service, verify_local_token
from app.core.resource_uri import file_uri_child
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_service import WorkspaceService

SESSION = "ses_019b781159d47374805bc4427d081a14"


@dataclass(frozen=True)
class RawCase:
    store: AttachmentBlobStore
    service: WorkspaceService
    client: httpx.AsyncClient
    ref: AttachmentRef
    content: bytes
    preview: str


@pytest.fixture
async def raw_case(empty_workspace_scope, session_bundle_factory) -> AsyncIterator[RawCase]:
    _, root = empty_workspace_scope
    session_bundle_factory(root / ".boxteam/sessions", SESSION)
    leases = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
    with closing(AttachmentBlobStore(root, leases=leases)) as store:
        content = (Path.cwd() / "tests/fixtures/workspaces/default_test_workspace/assets/test.jpg").read_bytes()
        ref, = await store.persist_inline(SESSION, [AttachmentRef(
            file_id="inline:media", name="原始 图片 %.png", content_type="image/jpeg",
            data_url="data:image/jpeg;base64," + base64.b64encode(content).decode("ascii"),
        )], thread_id=store._path_resolver.main_thread_id(SESSION), message_id="msg_raw_metadata")
        assert ref.directory_uri is not None
        store.read_thumbnail(SESSION, ref.file_id)
        preview = file_uri_child(ref.directory_uri, "preview", "512.webp")
        with closing(WorkspaceService(config_service=Mock(spec=ConfigService), workspace_root=root,
                                      attachments=store)) as service:
            application = FastAPI()
            application.add_middleware(TraceMiddleware)
            application.include_router(workspace.router, prefix="/api/v1")
            application.dependency_overrides[verify_local_token] = lambda: "local"
            application.dependency_overrides[get_workspace_service] = lambda: service
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://raw") as client:
                yield RawCase(store, service, client, ref, content, preview)


async def test_head_projects_original_and_preview_without_reading_body(raw_case: RawCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = raw_case
    assert case.ref.name is not None

    async def read_forbidden(*_args, **_kwargs):
        pytest.fail("HEAD 不得读取原文件正文")

    def store_read_forbidden(*_args, **_kwargs):
        pytest.fail("响应头不得经附件正文读取反推 MIME")

    with monkeypatch.context() as patch:
        patch.setattr(anyio.AsyncFile, "read", read_forbidden)
        patch.setattr(case.store, "read", store_read_forbidden)
        for uri, filename, mime, size in [(case.ref.file_id, case.ref.name, "image/jpeg", len(case.content)),
                                        (case.preview, "512.webp", "image/webp", None)]:
            response = await case.client.head("/api/v1/workspace/files/raw", params={"path": uri},
                                              headers={"X-Request-ID": "raw-head-metadata"})
            assert response.status_code == 200
            assert response.content == b""
            assert response.headers["content-type"] == mime
            assert response.headers["content-disposition"] == (
                f"inline; filename*=utf-8''{quote(filename)}" if filename == case.ref.name else 'inline; filename="512.webp"'
            )
            length = int(response.headers["content-length"])
            if size is not None:
                assert length == size
            else:
                assert length > 0
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["X-Request-ID"] == "raw-head-metadata"

    full = await case.client.get("/api/v1/workspace/files/raw", params={"path": case.ref.file_id})
    assert full.status_code == 200 and full.content == case.content
    assert full.headers["content-type"] == "image/jpeg"
    assert full.headers["cache-control"] == "private, no-cache"
    ranged = await case.client.get("/api/v1/workspace/files/raw", params={"path": case.ref.file_id},
                                   headers={"Range": "bytes=2-7"})
    assert ranged.status_code == 206 and ranged.content == case.content[2:8]
    assert ranged.headers["content-type"] == "image/jpeg"
    assert ranged.headers["content-range"] == f"bytes 2-7/{len(case.content)}"


async def test_head_and_get_reject_other_owner_directory_and_missing_file(raw_case: RawCase) -> None:
    case = raw_case
    other = case.ref.file_id.replace(case.service.workspace_id, "00000000-0000-4000-8000-000000000099")
    for path, status in [(other, 400), (case.ref.directory_uri, 400),
                         (file_uri_child(case.ref.directory_uri, "derived", "missing.png"), 404)]:
        for method in ("HEAD", "GET"):
            response = await case.client.request(method, "/api/v1/workspace/files/raw", params={"path": path})
            assert response.status_code == status, (method, path, response.text)
            assert response.headers["X-Request-ID"]


async def test_head_opens_for_permission_check_but_never_reads(raw_case: RawCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = raw_case
    target = case.service.prepare_file_download(path=case.ref.file_id, archive_directory=False).path
    original = anyio.open_file

    async def denied(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "HEAD 原生读取权限不足", str(target))
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(anyio, "open_file", denied)
    for method in ("HEAD", "GET"):
        response = await case.client.request(method, "/api/v1/workspace/files/raw", params={"path": case.ref.file_id},
                                             headers={"X-Request-ID": "raw-permission-denied"})
        assert response.status_code == 500
        assert response.headers["X-Request-ID"] == "raw-permission-denied"
