"""下载响应的异常因果、临时文件与取消清理。"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import anyio
import pytest

from app.api.workspace_file_response import WorkspaceFileResponse
from app.core.atomic_fs import native_io_path
from app.services.infrastructure.workspace_files.filesystem import PreparedFileDownload


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
        assert primary.__cause__ is native_error
        assert "stage=文件传输" in str(primary)
    assert cleanup.__cause__ is cleanup_error
    assert "stage=临时文件清理" in str(cleanup)
    assert str(download.path) not in str(primary) + str(cleanup)
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
    assert caught.value.__cause__.filename == str(download.path)
    assert str(download.path) not in str(caught.value)
