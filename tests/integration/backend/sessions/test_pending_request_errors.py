"""真实 HTTP 冷读损坏队列的诊断；不作为完整执行恢复验收。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def pending_error_target(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
):
    response = await integration_client.post("/api/v1/sessions", json={"title": "队列错误诊断"})
    assert response.status_code == 200, response.text
    session_id = response.json()["data"]["session_id"]
    root = Path(integration_workspace_root_path)
    catalog = root / ".boxteam" / "navigation" / "session-catalog.sqlite"
    # 仅查询产品创建的 locator，用于定点注入损坏；不替产品创建会话或恢复状态。
    connection = sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
    try:
        locator, = connection.execute(
            "SELECT storage_relative_locator FROM nodes WHERE node_id = ?", (session_id,),
        ).fetchone()
    finally:
        connection.close()
    path = root / ".boxteam" / locator / "pending_requests.json"
    assert path.parent.is_dir()
    try:
        yield session_id, path, root
    finally:
        if path.exists():
            path.chmod(0o644)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["header", "detail", "permission"])
async def test_pending_request_failure_does_not_publish_native_storage(
    integration_client: httpx.AsyncClient,
    pending_error_target: tuple[str, Path, Path],
    failure: str,
    request: pytest.FixtureRequest,
) -> None:
    session_id, path, root = pending_error_target
    if failure == "permission" and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("真实权限错误需要非 root POSIX 用户，Windows ACL 另行验收")
    empty = b'{"version":5,"request_count":0}\n[]\n'
    if failure == "header":
        corrupted = b"incomplete-header"
    elif failure == "detail":
        corrupted = b'{"version":5,"request_count":1}\n' + json.dumps([
            {str(path): str(root / "private-input.txt")},
        ]).encode() + b"\n"
    else:
        corrupted = empty
    path.write_bytes(corrupted)
    if failure == "permission":
        path.chmod(0)
    receipts = []
    for index, scope in enumerate(("", "/main-thread")):
        request_id = f"req_pending_error_{failure}_{index}"
        response = await integration_client.get(
            f"/api/v1/sessions/{session_id}{scope}/pending-requests",
            headers={"X-Request-ID": request_id},
        )
        assert response.status_code == 500, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        assert str(root) not in response.text
        assert "private-input.txt" not in response.text
        message = response.json()["message"]
        assert session_id in message and "artifact=pending_requests" in message
        assert "stage=read_control" in message
        assert "error=" in message
        if failure == "permission":
            assert "PermissionError" in message and "errno=13" in message
        receipts.append(response.json())
    path.chmod(0o644)
    assert path.read_bytes() == corrupted
    # 修复控制文件后重试原入口，读取失败不得提前标记为已恢复。
    path.write_bytes(empty)
    repaired = await integration_client.get(f"/api/v1/sessions/{session_id}/pending-requests")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["requests"] == []
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{failure}-pending-errors.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
