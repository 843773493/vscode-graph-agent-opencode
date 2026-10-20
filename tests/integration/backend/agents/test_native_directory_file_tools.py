"""真实模型工具装配访问未注册目录；Provider 由本地协议桩驱动。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from app.core.resource_uri import parse_resource_uri
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - fixture 沿当前文件隔离
)
from tests.support.api_helpers import create_session


@pytest.fixture(autouse=True)
def local_model_metadata(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")


async def test_model_tools_reopen_native_directory_without_registration(communication_case: CommunicationCase):
    case = communication_case
    external = case.workspace.parent / "模型 未注册%"
    external.mkdir(exist_ok=True)
    file = external / "中文 文件%.txt"
    file.write_text("native-directory-needle Ω\n", encoding="utf-8")
    version = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    blocked = external / "blocked.txt"
    blocked.unlink(missing_ok=True)
    decoy = case.workspace / str(file).lstrip("/")
    decoy.parent.mkdir(parents=True)
    decoy.write_text("WORKSPACE REDIRECT DECOY\n")
    database = case.workspace.parent / "boxteam-home/state/gateway/gateway.sqlite"

    def registry():
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()

    before = registry()
    session = await create_session(case.client, "模型读取未注册目录")
    tool_results = []

    def reply(payload):
        if not any(message["role"] == "user" and "native-directory-trigger" in str(message["content"])
                   for message in payload["messages"]):
            return {"content": "未注册目录验证"}
        results = [message for message in payload["messages"] if message["role"] == "tool"]
        if not results:
            return _tool("ls", {"path": str(external)}, "call_native_ls")
        listing = json.loads(results[0]["content"])
        entry, = listing["entries"]
        root = parse_resource_uri(listing["node"]["uri"])
        assert root.scope == "gateway" and root.scope_id == case.gateway_id and root.kind == "files"
        assert entry["uri"].startswith(root.display_uri + "/")
        if len(results) == 1:
            return _tool("glob", {"path": root.display_uri, "pattern": "*.txt"}, "call_native_glob")
        assert json.loads(results[1]["content"])["entries"] == [entry]
        if len(results) == 2:
            return _tool("read_file", {"path": str(file)}, "call_native_absolute_read")
        if len(results) == 3:
            return _tool("read_file", {"path": entry["uri"]}, "call_native_uri_read")
        assert results[2]["content"] == results[3]["content"]
        assert "1\tnative-directory-needle Ω" in results[3]["content"]
        assert version in results[3]["content"]
        if len(results) == 4:
            return _tool("grep", {"path": root.display_uri, "pattern": "native-directory-needle",
                                  "output_mode": "content"}, "call_native_grep")
        assert entry["uri"] in results[4]["content"] and "native-directory-needle Ω" in results[4]["content"]
        if len(results) == 5:
            return _tool("edit_file", {"file_path": str(file), "old_string": "needle", "new_string": "changed",
                                      "expected_version": version}, "call_native_edit_denied")
        if len(results) == 6:
            return _tool("write_file", {"file_path": str(blocked), "content": "blocked", "expected_version": "absent"},
                         "call_native_write_denied")
        if len(results) == 7:
            return _tool("apply_patch", {"target": str(external), "explanation": "核实工作区外写入能力拒绝",
                "input": f"*** Begin Patch\n*** Update File: {file.name}\n@@\n-native-directory-needle Ω\n+changed\n*** End Patch",
                "expected_versions": {file.name: version}}, "call_native_patch_denied")
        for message in results[5:]:
            denied = json.loads(message["content"])
            assert (denied["status"], denied["effect"], denied["owner"], denied["code"], denied["retryable"]) == (
                "error", "not_executed", "resource_uri_resolver", "capability_denied", False), denied
        wire = json.dumps(results, ensure_ascii=False)
        assert str(external) not in wire and "WORKSPACE REDIRECT DECOY" not in wire
        tool_results[:] = results
        return {"content": "未注册目录文件工具验证完成"}

    case.model.response_callback = reply
    accepted = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "native-directory-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    await _completed(case.client, accepted.json()["data"]["job_id"])
    assert len(tool_results) == 8
    assert file.read_text() == "native-directory-needle Ω\n" and decoy.read_text() == "WORKSPACE REDIRECT DECOY\n"
    assert not blocked.exists() and not (decoy.parent / blocked.name).exists()
    changes = await case.client.get(f"/api/v1/sessions/{session}/changesets/all")
    assert changes.status_code == 200, changes.text
    assert changes.json()["data"]["files"] == []
    assert registry() == before
    assert not (external / ".boxteam").exists()
    case.record("native-directory-tools", {"results": tool_results, "registry_before": before, "registry_after": registry()})
