"""下载响应的异常因果、临时文件与取消清理。"""

from __future__ import annotations

import asyncio
import errno
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from starlette.datastructures import Headers
from starlette.responses import Response

from app.api.workspace_file_response import WorkspaceFileResponse
from app.core.atomic_fs import native_io_path
from app.core.http_cache import NoStoreErrorsMiddleware
from app.services.infrastructure.workspace_files.filesystem import (
    PreparedFileDownload,
    file_access_error_detail,
)


@pytest.fixture
def download(tmp_path):
    target = tmp_path / "archive.zip"
    target.write_bytes(b"archive-content")
    return PreparedFileDownload(target, "boxteam://workspace/ws-test/resources/files/root/bundle",
                                "bundle.zip", "application/zip", target.stat(), temporary=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["open", "read", "close", "cancel"])
async def test_transfer_and_cleanup_failures_keep_both_causes(download, monkeypatch, failure):
    physical = native_io_path(download.path)
    original_open, original_read, original_close = anyio.open_file, anyio.AsyncFile.read, anyio.AsyncFile.aclose
    native_error = OSError(errno.EIO, f"native transfer failed at {download.path}", str(download.path))
    cleanup_error = PermissionError(errno.EACCES, f"native cleanup failed at {download.path}", str(download.path))

    async def open_file(path, *args, **kwargs):
        if Path(path) == physical and failure == "open":
            raise native_error
        return await original_open(path, *args, **kwargs)

    async def read(file, *args):
        if Path(file.wrapped.name) == physical and failure == "read":
            raise native_error
        return await original_read(file, *args)

    async def close(file):
        await original_close(file)
        if Path(file.wrapped.name) == physical and failure == "close":
            raise native_error

    def unlink(path, *args, **kwargs):
        assert path == physical
        raise cleanup_error

    monkeypatch.setattr(anyio, "open_file", open_file)
    monkeypatch.setattr(anyio.AsyncFile, "read", read)
    monkeypatch.setattr(anyio.AsyncFile, "aclose", close)
    monkeypatch.setattr(Path, "unlink", unlink)
    sent = []
    cancellation = asyncio.CancelledError("caller disconnected")

    async def send(message):
        sent.append(message)
        if failure == "cancel" and message["type"] == "http.response.body":
            raise cancellation

    async def receive():
        return {"type": "http.disconnect"}

    response = WorkspaceFileResponse(download, disposition="attachment")
    scope = {"type": "http", "method": "GET", "headers": [], "extensions": {"http.response.pathsend": {}}}
    with pytest.raises(BaseExceptionGroup) as caught:
        await response(scope, receive, send)
    primary, cleanup = caught.value.exceptions
    if failure == "cancel":
        assert primary is cancellation
    else:
        assert primary is native_error and primary.__cause__ is None
        assert "stage=文件传输" in file_access_error_detail(primary)
    assert cleanup is cleanup_error and cleanup.__cause__ is None
    assert "stage=临时文件清理" in file_access_error_detail(cleanup)
    assert str(download.path) in str(cleanup)
    assert download.path.read_bytes() == b"archive-content"
    assert all(message["type"] != "http.response.pathsend" for message in sent)
    if failure in {"open", "read"}:
        assert sent == []


@pytest.mark.asyncio
async def test_failed_transfer_removes_temporary_archive(download, monkeypatch):
    async def fail_open(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "gone", str(download.path))

    monkeypatch.setattr(anyio, "open_file", fail_open)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(FileNotFoundError) as caught:
        await WorkspaceFileResponse(download, disposition="attachment")(
            {"type": "http", "method": "GET", "headers": []}, receive, send,
        )
    assert not download.path.exists()
    assert sent == []
    assert caught.value.errno == errno.ENOENT
    assert caught.value.filename == str(download.path) and caught.value.strerror == "gone"
    assert caught.value.__cause__ is None
    assert download.uri in file_access_error_detail(caught.value)


@pytest.fixture
def preview(download):
    return replace(download, temporary=False)


@pytest.fixture
def transfer():
    async def capture(response, *, headers=(), path="/api/v1/workspace/files/raw"):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.disconnect"}

        await response({"type": "http", "method": "GET", "path": path, "headers": list(headers)}, receive, send)
        start = sent[0]
        return start["status"], Headers(raw=start["headers"]), b"".join(message.get("body", b"") for message in sent[1:])

    return capture


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["weak", "strong", "list", "star"])
async def test_preview_revalidation_checks_read_access_without_reading_body(preview, transfer, monkeypatch, condition):
    status, headers, body = await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"))
    assert status == 200 and body == b"archive-content"
    assert headers["cache-control"] == "private, no-cache"
    vary = {
        "x-boxteam-workspace-id", "x-boxteam-target-gateway-id", "x-boxteam-gateway-id",
        "x-local-token", "x-boxteam-federation-token", "cookie",
    }
    assert {part.strip().lower() for part in headers["vary"].split(",")} == vary
    etag = headers["etag"]
    assert etag.startswith('W/"')
    conditions = {"weak": etag, "strong": etag[2:], "list": '"old", ' + etag, "star": "*"}
    opened = []
    original_open = anyio.open_file

    async def open_file(path, *args, **kwargs):
        opened.append(path)
        return await original_open(path, *args, **kwargs)

    async def forbidden_read(*args, **kwargs):
        pytest.fail("304 不得读取正文")

    monkeypatch.setattr(anyio, "open_file", open_file)
    monkeypatch.setattr(anyio.AsyncFile, "read", forbidden_read)
    response = WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a")
    response.headers["Vary"] = "Accept-Encoding"
    status, reused, body = await transfer(response, headers=[(b"if-none-match", conditions[condition].encode())])
    assert status == 304 and body == b""
    assert len(opened) == 1
    assert reused["etag"] == etag and reused["cache-control"] == headers["cache-control"]
    assert {part.strip().lower() for part in reused["vary"].split(",")} == vary | {"accept-encoding"}
    assert "content-length" not in reused


@pytest.mark.asyncio
async def test_preview_validator_does_not_hide_read_permission_failure(preview, transfer, monkeypatch):
    _, headers, _ = await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"))
    native = PermissionError(errno.EACCES, "read denied", str(preview.path))

    async def deny(*args, **kwargs):
        raise native

    monkeypatch.setattr(anyio, "open_file", deny)
    with pytest.raises(PermissionError) as caught:
        await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"),
                       headers=[(b"if-none-match", headers["etag"].encode())])
    assert caught.value is native and native.filename == str(preview.path)


@pytest.mark.asyncio
async def test_preview_validator_isolated_by_owner_uri_and_current_observation(preview, transfer):
    _, first, _ = await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"))
    changed_uri = replace(preview, uri=preview.uri + "-other")
    preview.path.write_bytes(b"changed-content")
    changed = replace(preview, metadata=preview.path.stat())
    for owner, item in [("owner-b", preview), ("owner-a", changed_uri), ("owner-a", changed)]:
        status, headers, body = await transfer(WorkspaceFileResponse(item, disposition="inline", preview_owner=owner),
            headers=[(b"if-none-match", first["etag"].encode())])
        assert status == 200 and body == b"changed-content"
        assert headers["etag"] != first["etag"]


@pytest.mark.asyncio
async def test_ranges_downloads_and_temporary_data_remain_no_store(download, preview, transfer):
    _, first, _ = await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"))
    status, headers, body = await transfer(WorkspaceFileResponse(preview, disposition="inline", preview_owner="owner-a"),
        headers=[(b"range", b"bytes=0-6"), (b"if-none-match", first["etag"].encode())])
    assert status == 206 and body == b"archive"
    assert headers["content-range"] == "bytes 0-6/15" and headers["cache-control"] == "no-store"
    for item, disposition in [(preview, "attachment"), (download, "inline")]:
        status, headers, body = await transfer(WorkspaceFileResponse(item, disposition=disposition, preview_owner="owner-a"),
            headers=[(b"if-none-match", first["etag"].encode())])
        assert status == 200 and body == b"archive-content"
        assert headers["cache-control"] == "no-store"
    assert not download.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 304, 400, 401, 403, 404, 416, 500, 503])
async def test_error_policy_only_changes_selected_resource_failures(status, transfer):
    response = Response(status_code=status, headers={"Cache-Control": "private, no-cache"})
    policy = NoStoreErrorsMiddleware(response, paths=(
        "/api/v1/workspace/files/raw", "/api/v1/workspaces/{workspace_id}/workspace/files/raw",
    ))
    for path in ("/api/v1/workspace/files/raw", "/api/v1/workspaces/owner-a/workspace/files/raw"):
        _, headers, _ = await transfer(policy, path=path)
        assert headers["cache-control"] == ("no-store" if status >= 400 else "private, no-cache")
    _, unrelated, _ = await transfer(policy, path="/api/v1/other")
    assert unrelated["cache-control"] == "private, no-cache"
