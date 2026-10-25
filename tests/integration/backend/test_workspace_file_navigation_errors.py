"""经真实文件 owner 与 ASGI 路由验证原生错误、受理及原请求重试。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import resources as resources_api
from app.api import workspace as workspace_api
from app.api.deps import get_text_mutations, get_workspace_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files import operations
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def file_case(request: pytest.FixtureRequest, integration_workspace_root_path: str):
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = Path(integration_workspace_root_path)
    directory = root / ("navigation-" + uuid4().hex)
    directory.mkdir()
    (directory / "seed.txt").write_bytes(b"original\n")
    config = ConfigService(workspace_root=root, config_dir=context.runtime_root_for_node(request.node.nodeid) / "config")
    service = WorkspaceService(config_service=config, workspace_root=root)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, workspace_http_exception_handler)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.include_router(resources_api.router, prefix="/api/v1")
    text_owner = WorkspaceTextMutationOwner(paths=service.file_paths)
    app.dependency_overrides[get_text_mutations] = lambda: text_owner
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "local-test"
    await service.file_operations.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://files.test") as client:
            listed = await client.get("/api/v1/workspace/files", params={"path": str(directory)})
            assert listed.status_code == 200, listed.text
            yield SimpleNamespace(root=root, directory=directory, service=service, client=client,
                                  listing=listed.json()["data"], artifacts=context.artifacts_dir)
    finally:
        await service.file_operations.stop()
        service.close()
        await config.close()


def file_rows(case):
    with closing(sqlite3.connect(case.root / ".boxteam/navigation/workspace-files.sqlite")) as db:
        return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                for name in ("owner", "directories", "operations", "events")}


async def test_directory_native_messages_stay_in_logs_and_original_listing_survives(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    before = file_rows(case)
    scandir = os.scandir
    evidence = []
    for number, status in ((errno.EACCES, 403), (errno.ENOENT, 404), (errno.ENOTDIR, 400)):
        for filename_field in ("first", "second"):
            original = OSError(number, f"private-strerror: {case.directory}")
            # CPython 在 filename=None 时会忽略构造参数 filename2；分别设置以覆盖两个真实属性。
            if filename_field == "first":
                original.filename = str(case.directory)
            else:
                original.filename2 = str(case.directory / "private-second")

            def fail(path, error=original):
                if Path(path) == case.directory:
                    raise error
                return scandir(path)

            request_id = "req-file-navigation-" + str(len(evidence))
            with monkeypatch.context() as patch:
                patch.setattr(os, "scandir", fail)
                failed = await case.client.get("/api/v1/workspace/files", params={"path": case.listing["path"]},
                                               headers={"X-Request-ID": request_id})
            assert failed.status_code == status, failed.text
            body = failed.json()
            assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
            assert str(case.directory) not in failed.text and "private-strerror" not in failed.text
            assert type(original).__name__ in body["detail"] and f"errno={number}" in body["detail"]
            assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
            assert file_rows(case) == before
            evidence.append(body)
    assert "private-strerror" in caplog.text and str(case.directory) in caplog.text
    continued = await case.client.get("/api/v1/workspace/files", params={"path": case.listing["path"]})
    assert continued.json()["data"] == case.listing
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "directory-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


async def test_real_upload_staging_collision_keeps_409_original_state_and_same_id_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    operation_id = "op_" + uuid4().hex
    fixed = uuid4()
    collision = case.root / ".boxteam/navigation/workspace-files-staging" / (operation_id + "-" + fixed.hex)
    collision.mkdir(parents=True)
    marker = collision / "foreign.txt"
    marker.write_bytes(b"do not remove\n")
    intent = {"client_operation_id": operation_id, "client_sequence": 1,
              "directory_key": case.listing["directory_key"],
              "expected_directory_revision": case.listing["directory_revision"]}
    before = file_rows(case)

    async def upload():
        return await case.client.post("/api/v1/workspace/files/upload", params={"path": case.listing["path"]},
            data={"intent": json.dumps(intent), "relative_paths": "uploaded.txt"},
            files={"files": ("uploaded.txt", b"payload\n", "text/plain")},
            headers={"X-Request-ID": "req-upload-collision"})

    with monkeypatch.context() as patch:
        patch.setattr(operations, "uuid4", lambda: fixed)
        failed = await upload()
    assert failed.status_code == 409, failed.text
    body = failed.json()
    assert body["request_id"] == failed.headers["X-Request-ID"] == "req-upload-collision"
    assert "FileExistsError" in body["detail"] and f"errno={errno.EEXIST}" in body["detail"]
    assert str(collision) not in failed.text and str(case.root) not in failed.text
    assert str(collision) in caplog.text
    assert file_rows(case) == before
    assert marker.read_bytes() == b"do not remove\n"
    assert not (case.directory / "uploaded.txt").exists()
    marker.unlink()
    collision.rmdir()
    accepted = await upload()
    assert accepted.status_code == 202, accepted.text
    async with asyncio.timeout(10):
        while True:
            result = await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": operation_id, "expected_workspace_id": case.service.workspace_id,
            })
            assert result.status_code == 200, result.text
            receipt = result.json()["data"]["operations"][0]
            assert receipt["state"] in {"queued", "running", "committed"}, receipt
            if receipt["state"] == "committed":
                break
            await asyncio.sleep(0.02)
    assert receipt["effect"] == "complete"
    assert (case.directory / "uploaded.txt").read_bytes() == b"payload\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "upload-collision.json").write_text(json.dumps({
        "failure": body, "accepted": accepted.json(), "receipt": receipt,
    }, ensure_ascii=False, indent=2) + "\n")


async def test_text_preflight_uses_shared_error_and_creates_nothing_until_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    target = case.directory / "new.txt"
    uri = case.listing["path"] + "/new.txt"
    original = OSError(errno.EIO, f"private-strerror: {target}", str(target))
    resolve = Path.resolve
    before = file_rows(case)
    payload = {"create": {"target": uri, "content": "text payload\n", "expected_version": "absent"}}

    def fail(path, *args, **kwargs):
        if path == target:
            raise original
        return resolve(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", fail)
        failed = await case.client.post("/api/v1/resources/text-mutations", json=payload,
                                       headers={"X-Request-ID": "req-text-preflight"})
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["request_id"] == failed.headers["X-Request-ID"] == "req-text-preflight"
    receipt = body["data"]["receipt"]
    assert (receipt["status"], receipt["effect"], receipt["code"], receipt["owner"]) == (
        "error", "not_executed", "execution_failed", "resource_file_operations",
    )
    assert receipt["error_type"] == "OSError" and f"errno={errno.EIO}" in receipt["error"]
    assert not receipt["journal_id"] and not receipt["files"]
    assert str(case.root) not in failed.text and "private-strerror" not in failed.text
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert str(target) in caplog.text and "private-strerror" in caplog.text
    assert file_rows(case) == before and not target.exists()
    saved = await case.client.post("/api/v1/resources/text-mutations", json=payload)
    assert saved.status_code == 200, saved.text
    result = saved.json()["data"]["receipt"]
    assert (result["status"], result["effect"]) == ("success", "completed")
    assert result["files"][0]["path"] == uri and result["files"][0]["state"] == "published"
    assert target.read_bytes() == b"text payload\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "text-preflight.json").write_text(json.dumps({
        "failure": body, "saved": saved.json(),
    }, ensure_ascii=False, indent=2) + "\n")
