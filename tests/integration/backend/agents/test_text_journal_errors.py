"""真实文本传输入口在 journal 创建失败时保持未发布，并可修复重试。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from app.schemas.internal_v2.text_mutation import EditText, TextMutationRequest
from tests.harness.python.run_context import TestRunContext


@pytest.mark.asyncio
async def test_text_journal_create_permission_keeps_file_and_recovers(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
    request: pytest.FixtureRequest,
):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("真实 journal 权限错误需要非 root POSIX 用户，Windows ACL 单独验收")
    root = Path(integration_workspace_root_path)
    target = root / "journal-notes.txt"
    target.write_bytes(b"before\n")
    reference = await integration_client.post("/api/v1/workspace/files/references", json={"paths": [str(target)]})
    assert reference.status_code == 200, reference.text
    uri, = reference.json()["data"]["uris"]

    async def edit(before: str, after: str, request_id: str):
        command = EditText(target=uri, old_string=before, new_string=after,
                           expected_version="sha256:" + hashlib.sha256((before + "\n").encode()).hexdigest())
        return await integration_client.post("/api/v1/resources/text-mutations",
            json=TextMutationRequest.from_command(command).model_dump(mode="json", exclude_none=True),
            headers={"X-Request-ID": request_id})

    initial = await edit("before", "middle", "req_journal_initial")
    assert initial.status_code == 200 and initial.json()["data"]["receipt"]["status"] == "success", initial.text
    assert target.read_bytes() == b"middle\n"
    journal_root = root / ".boxteam/cache/text_mutations"
    existing = {path.name: path.read_bytes() for path in journal_root.iterdir()}
    try:
        journal_root.chmod(0o555)
        response = await edit("middle", "after", "req_journal_permission")
    finally:
        journal_root.chmod(0o755)
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "req_journal_permission"
    receipt = response.json()["data"]["receipt"]
    assert receipt["status"] == "error" and receipt["effect"] == "not_executed" and receipt["retryable"] is False
    assert receipt["error_type"] == "PermissionError" and receipt["files"] == []
    assert "artifact=text_mutation_journal" in receipt["error"] and "errno=13" in receipt["error"]
    assert str(root) not in response.text and receipt["journal_id"] is None
    assert target.read_bytes() == b"middle\n"
    assert {path.name: path.read_bytes() for path in journal_root.iterdir()} == existing
    repaired = await edit("middle", "after", "req_journal_repaired")
    assert repaired.status_code == 200 and repaired.json()["data"]["receipt"]["status"] == "success", repaired.text
    assert target.read_bytes() == b"after\n"
    snapshot, = repaired.json()["data"]["snapshots"]
    assert snapshot["uri"] == uri and snapshot["before_content"] == "middle\n" and snapshot["after_content"] == "after\n"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "journal-permission.json").write_text(json.dumps({
        "initial": initial.json(), "error": response.json(), "repaired": repaired.json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
