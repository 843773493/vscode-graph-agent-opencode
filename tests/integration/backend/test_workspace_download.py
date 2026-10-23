"""真实 HTTP 文件下载的准备、延迟读取与临时归档清理合同。"""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import anyio
import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.api import workspace as workspace_api
from app.api.deps import get_workspace_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure import workspace_service as service_module
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files.paths import file_uri_child
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.support.ports import integration_port_block_for_file


@pytest.fixture
async def download_case(tmp_path, monkeypatch, request):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "文件 空格%.txt"
    target.write_bytes(bytes(range(256)) * 600)
    service = WorkspaceService(config_service=Mock(spec=ConfigService), workspace_root=workspace)
    archive_root = tmp_path / "downloads"
    archive_root.mkdir()
    archives = []
    mkstemp = service_module.tempfile.mkstemp

    def create_archive(**kwargs):
        fd, name = mkstemp(dir=archive_root, **kwargs)
        archives.append(Path(name))
        return fd, name

    monkeypatch.setattr(service_module.tempfile, "mkstemp", create_archive)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False,
        log_config=None, lifespan="off",
    ))
    task = asyncio.create_task(asyncio.to_thread(server.run))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
            yield SimpleNamespace(
                client=client, service=service, workspace=workspace, target=target,
                uri=file_uri_child(service.file_paths.workspace_uri, target.name), archives=archives,
            )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        service.close()


def assert_failure(response, case, *, stage, request_id):
    assert response.status_code == 500, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"] == request_id
    assert f"stage={stage}" in body["message"]
    assert str(case.workspace) not in response.text
    assert all(str(path) not in response.text for path in case.archives)
    return body["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["raw", "download"])
async def test_download_native_and_uri_inputs_preserve_bytes_headers_and_ranges(download_case, endpoint):
    case = download_case
    original = case.target.read_bytes()
    for value in (case.target.name, str(case.target), case.uri):
        response = await case.client.get(f"/api/v1/workspace/files/{endpoint}", params={"path": value})
        assert response.status_code == 200, response.text
        assert response.content == original
        assert response.headers["content-length"] == str(len(original))
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["accept-ranges"] == "bytes"
        disposition = "inline" if endpoint == "raw" else "attachment"
        assert response.headers["content-disposition"] == f"{disposition}; filename*=utf-8''{quote(case.target.name)}"
        ranged = await case.client.get(
            f"/api/v1/workspace/files/{endpoint}", params={"path": value}, headers={"Range": "bytes=2-7"},
        )
        assert ranged.status_code == 206
        assert ranged.content == original[2:8]
        assert ranged.headers["content-range"] == f"bytes 2-7/{len(original)}"
    multiple = await case.client.get(
        f"/api/v1/workspace/files/{endpoint}", params={"path": case.uri}, headers={"Range": "bytes=0-2,10-12"},
    )
    assert multiple.status_code == 206
    assert multiple.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert b"\x00\x01\x02\r\n" in multiple.content
    assert b"\x0a\x0b\x0c\r\n" in multiple.content
    assert int(multiple.headers["content-length"]) == len(multiple.content)
    assert case.target.read_bytes() == original


@pytest.mark.asyncio
async def test_directory_zip_and_invalid_ranges_always_remove_temporary_download(download_case):
    case = download_case
    directory = case.workspace / "目录 空格%"
    directory.mkdir()
    (directory / "内容.txt").write_bytes(b"original\x00\xff")
    (directory / "empty").mkdir()
    response = await case.client.get("/api/v1/workspace/files/download", params={"path": str(directory)})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read(f"{directory.name}/内容.txt") == b"original\x00\xff"
        assert f"{directory.name}/empty/" in archive.namelist()
    for value, expected in (("nonsense", 400), ("bytes=999999-", 416)):
        rejected = await case.client.get(
            "/api/v1/workspace/files/download", params={"path": str(directory)}, headers={"Range": value},
        )
        assert rejected.status_code == expected
    raw_directory = await case.client.get("/api/v1/workspace/files/raw", params={"path": str(directory)})
    assert raw_directory.status_code == 400
    assert case.archives and all(not path.exists() for path in case.archives)
    assert (directory / "内容.txt").read_bytes() == b"original\x00\xff"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["raw", "download"])
async def test_download_stat_error_keeps_native_cause_in_diagnostics(download_case, endpoint, monkeypatch, caplog):
    case = download_case
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == case.target and kwargs.get("follow_symlinks") is not False:
            raise PermissionError(errno.EACCES, f"stat denied {case.target}", str(case.target))
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(Path, "stat", stat)
        response = await case.client.get(
            f"/api/v1/workspace/files/{endpoint}", params={"path": case.uri}, headers={"X-Request-ID": "req-download-stat"},
        )
    message = assert_failure(response, case, stage="文件检查", request_id="req-download-stat")
    assert "PermissionError" in message and f"errno={errno.EACCES}" in message
    assert case.uri in message
    assert str(case.target) in caplog.text
    assert case.target.read_bytes() == bytes(range(256)) * 600


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["临时文件创建", "目录归档", "归档检查"])
async def test_archive_preparation_failure_cleans_temp_and_preserves_source(download_case, stage, monkeypatch, caplog):
    case = download_case
    directory = case.workspace / "bundle"
    directory.mkdir()
    source = directory / "source.txt"
    source.write_bytes(b"original")
    original_stat = Path.stat

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, f"archive I/O failed at {source}", str(source))

    def stat(path, *args, **kwargs):
        if path in case.archives:
            return fail()
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        if stage == "临时文件创建":
            patch.setattr(service_module.tempfile, "mkstemp", fail)
        elif stage == "目录归档":
            patch.setattr(zipfile.ZipFile, "write", fail)
        else:
            patch.setattr(Path, "stat", stat)
        response = await case.client.get(
            "/api/v1/workspace/files/download", params={"path": str(directory)}, headers={"X-Request-ID": "req-archive-prep"},
        )
    message = assert_failure(response, case, stage=stage, request_id="req-archive-prep")
    assert f"errno={errno.EIO}" in message
    assert str(source) in caplog.text
    assert all(not path.exists() for path in case.archives)
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["raw", "download"])
@pytest.mark.parametrize("operation", ["open", "read"])
async def test_delayed_file_access_failure_returns_safe_error_and_can_retry(download_case, endpoint, operation, monkeypatch, caplog):
    case = download_case
    original_open = anyio.open_file
    original_read = anyio.AsyncFile.read

    async def open_file(path, *args, **kwargs):
        if Path(path) == case.target:
            raise PermissionError(errno.EACCES, f"open denied {path}", str(path))
        return await original_open(path, *args, **kwargs)

    async def read(file, *args):
        if Path(file.wrapped.name) == case.target:
            raise OSError(errno.EIO, f"read failed {case.target}", str(case.target))
        return await original_read(file, *args)

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(anyio, "open_file", open_file) if operation == "open" else patch.setattr(anyio.AsyncFile, "read", read)
        response = await case.client.get(
            f"/api/v1/workspace/files/{endpoint}", params={"path": case.uri}, headers={"X-Request-ID": "req-delayed-download"},
        )
    message = assert_failure(response, case, stage="文件传输", request_id="req-delayed-download")
    assert case.uri in message
    assert str(case.target) in caplog.text
    retried = await case.client.get(f"/api/v1/workspace/files/{endpoint}", params={"path": case.uri})
    assert retried.status_code == 200
    assert retried.content == case.target.read_bytes()


@pytest.mark.asyncio
async def test_midstream_read_failure_aborts_http_without_appending_error_body(download_case, monkeypatch, caplog):
    case = download_case
    original_read = anyio.AsyncFile.read
    reads = 0

    async def read(file, *args):
        nonlocal reads
        if Path(file.wrapped.name) == case.target:
            reads += 1
            if reads == 2:
                raise OSError(errno.EIO, f"midstream failed {case.target}", str(case.target))
        return await original_read(file, *args)

    chunks = []
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(anyio.AsyncFile, "read", read)
        async with case.client.stream("GET", "/api/v1/workspace/files/raw", params={"path": case.uri}) as response:
            assert response.status_code == 200
            with pytest.raises(httpx.RemoteProtocolError):
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
    assert b"".join(chunks) == case.target.read_bytes()[:65536]
    assert str(case.target) in caplog.text


@pytest.mark.asyncio
async def test_real_permission_denial_keeps_source_and_download_target(download_case, caplog):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("真实 chmod EACCES 需要非特权 POSIX 用户")
    case = download_case
    mode = case.target.stat().st_mode
    try:
        case.target.chmod(0)
        with caplog.at_level(logging.ERROR):
            response = await case.client.get(
                "/api/v1/workspace/files/raw", params={"path": str(case.target)}, headers={"X-Request-ID": "req-real-eacces"},
            )
        message = assert_failure(response, case, stage="文件传输", request_id="req-real-eacces")
        assert "PermissionError" in message and f"errno={errno.EACCES}" in message
        assert str(case.target) in caplog.text
    finally:
        case.target.chmod(mode)
    assert case.target.read_bytes() == bytes(range(256)) * 600
