"""文件owner启动失败后，同一服务立即恢复并经原HTTP入口执行文件操作。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from traceback import walk_tb
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import workspace as workspace_api
from app.api.deps import get_workspace_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files.store import WorkspaceFileStore
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
async def startup_case(integration_workspace_root_path):
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path) / ("startup-" + uuid4().hex)
    prepare_test_workspace(workspace_root=workspace,
                           template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
                           template_items=())
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 8192
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    with closing(WorkspaceFileStore(workspace, service.workspace_id)) as seed:
        database = seed.path
    baseline = database.read_bytes()
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "测试已认证local-token"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://files.test") as client:
            yield SimpleNamespace(service=service, database=database, baseline=baseline,
                                  client=client, workspace=workspace, artifacts=context.artifacts_dir)
    finally:
        await service.stop()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["version", "missing_table", "owner", "invalid_database"])
async def test_startup_failure_releases_real_connection_before_retrying_same_service(startup_case, failure):
    case = startup_case
    if failure == "invalid_database":
        case.database.write_bytes(b"invalid sqlite database")
    else:
        with closing(sqlite3.connect(case.database, isolation_level=None)) as connection:
            if failure == "version":
                connection.execute("PRAGMA user_version=99")
            elif failure == "missing_table":
                connection.execute("DROP TABLE events")
            else:
                connection.execute("UPDATE owner SET workspace_id=?", (str(case.workspace / "private-owner"),))
    before = case.database.read_bytes()
    expected = sqlite3.DatabaseError if failure == "invalid_database" else RuntimeError
    with pytest.raises(expected) as caught:
        await case.service.start()
    error = caught.value
    failed_store = next(frame.f_locals["self"] for frame, _ in walk_tb(error.__traceback__)
                        if frame.f_code is WorkspaceFileStore.__init__.__code__)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_store._db.execute("SELECT 1")
    assert case.service._file_operations is None
    assert str(case.workspace) not in str(error)
    quarantine = case.database.with_suffix(".rejected")
    case.database.rename(quarantine)  # Windows也必须能立即移动，不能依赖进程退出或GC。
    assert quarantine.read_bytes() == before
    case.database.write_bytes(case.baseline)
    await case.service.start()
    listing = await case.client.get("/api/v1/workspace/files")
    assert listing.status_code == 200, listing.text
    snapshot = listing.json()["data"]
    operation_id = "op_" + uuid4().hex
    accepted = await case.client.post("/api/v1/workspace/files/entries", json={
        "name": "recovered.txt", "kind": "file", "intent": {
            "client_operation_id": operation_id, "client_sequence": 1,
            "directory_key": snapshot["directory_key"],
            "expected_directory_revision": snapshot["directory_revision"],
        },
    }, headers={"X-Request-ID": "req_file_initialization_retry"})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["request_id"] == accepted.headers["X-Request-ID"] == "req_file_initialization_retry"
    async with asyncio.timeout(10):
        while True:
            status = await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": operation_id, "expected_workspace_id": case.service.workspace_id,
            })
            assert status.status_code == 200, status.text
            receipt = status.json()["data"]["operations"][0]
            assert receipt["state"] in {"queued", "running", "committed"}, receipt
            if receipt["state"] == "committed":
                break
            await asyncio.sleep(0.01)
    opened = await case.client.get("/api/v1/workspace/files/content", params={"path": receipt["created_path"]})
    assert opened.status_code == 200, opened.text
    assert opened.json()["data"]["content"] == ""
    assert (case.workspace / "recovered.txt").read_bytes() == b""
    assert case.service.file_operations.store.events(0)[-1]["operation_id"] == operation_id
    (case.artifacts / f"{failure}.json").write_text(json.dumps({
        "startup_error": str(error), "request": accepted.json(), "receipt": receipt, "opened": opened.json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
