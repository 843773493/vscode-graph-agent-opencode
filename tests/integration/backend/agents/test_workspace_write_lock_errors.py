"""真实 HTTP 正文保存遇到损坏写锁后，公开错误和同目标恢复。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.mark.asyncio
async def test_http_save_lock_open_failure_keeps_uri_and_original_then_recovers(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
    request: pytest.FixtureRequest,
):
    root = Path(integration_workspace_root_path)
    target = root / "write-lock-product.txt"
    original = b"original\r\n"
    target.write_bytes(original)
    response = await integration_client.get("/api/v1/workspace/files/content", params={"path": str(target)})
    assert response.status_code == 200, response.text
    before = response.json()["data"]
    assert before["path"].startswith("boxteam://")
    lock_path = root / ".boxteam/locks/files.write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    backup = lock_path.with_suffix(".test-backup")
    if lock_path.exists():
        lock_path.rename(backup)
    lock_path.mkdir()
    try:
        failed = await integration_client.put("/api/v1/workspace/files/content", params={"path": str(target)},
            json={"content": "new\n", "expected_revision": before["revision"]})
        assert failed.status_code == 400, failed.text
        assert failed.headers["X-Request-ID"]
        assert "stage=open" in failed.text and "artifact=workspace_file_lock" in failed.text
        assert str(root) not in failed.text and "files.write.lock" not in failed.text
        assert target.read_bytes() == original
    finally:
        lock_path.rmdir()
        if backup.exists():
            backup.rename(lock_path)
    saved = await integration_client.put("/api/v1/workspace/files/content", params={"path": before["path"]},
        json={"content": "new\n", "expected_revision": before["revision"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["path"] == before["path"]
    assert target.read_bytes() == b"new\n"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "write-lock-http.json").write_text(json.dumps({
        "before": before, "failure": failed.json(), "saved": saved.json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
