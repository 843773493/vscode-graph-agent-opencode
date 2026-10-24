"""真实 Gateway、PTY 和产品 fork 串联附件处理与冷恢复。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import pytest

from app.core.resource_uri import file_uri_child, parse_resource_uri
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    _fork,
)
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True)
async def test_upload_shell_read_fork_and_restart_share_original_directory(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "附件完整处理来源")
    await case.plan(session, [{"content": "附件已接受"}])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "保留原件并处理附件", "attachments": [
            upload(b"original-for-product-flow").model_dump(mode="json"),
        ]}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    history = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
    assert history.status_code == 200, history.text
    original, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    derived = file_uri_child(original["directory_uri"], "derived", "result.txt")
    program = (
        "from pathlib import Path; "
        "text=Path('../original/content').read_text(encoding='utf-8'); "
        "Path('result.txt').write_text(text+' processed',encoding='utf-8'); "
        "print('attachment-processing-complete')"
    )
    arguments = [sys.executable, "-c", program]
    calls = await case.plan(session, [
        _tool("exec_command", {
            "cmd": subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments),
            "workdir": file_uri_child(original["directory_uri"], "derived"), "yield_time_ms": 1000,
        }, "process-original"),
        _tool("read_file", {"path": derived}, "read-derived"),
        {"content": "派生文件已读取"},
    ])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "在目录中处理原件并读取结果"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    shell = case.tool_result(session, calls[0] + ":tool-call:process-original")
    assert shell["tool_outcome"] == "success" and shell["execution_confirmed"] is True, shell
    assert json.loads(shell["content"])["exit_code"] == 0
    assert "attachment-processing-complete" in shell["content"]
    read = case.tool_result(session, calls[1] + ":tool-call:read-derived")
    assert read["tool_outcome"] == "success" and read["execution_confirmed"] is True, read
    assert "original-for-product-flow processed" in read["content"], read
    saved = await case.client.get("/api/v1/workspace/files/content", params={"path": derived})
    assert saved.status_code == 200 and saved.json()["data"]["content"] == "original-for-product-flow processed", saved.text
    source_canonical = (case.main_thread_node(session) / "rollout/rollout.jsonl").read_bytes()
    point = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"mode": "full_rollout_copy"})
    assert point.status_code == 200, point.text
    fork = await _fork(case.client, session, point.json()["data"], mode="full_rollout_copy", key="attachment-product-flow")
    target = fork["session"]["session_id"]
    assert target != session
    assert parse_resource_uri(fork["source_uri"]).gateway_authority == case.gateway_id
    assert parse_resource_uri(fork["source_uri"], local_gateway_id=case.gateway_id) == parse_resource_uri(
        point.json()["data"]["source_uri"], local_gateway_id=case.gateway_id,
    )
    assert (case.main_thread_node(target) / "rollout/rollout.jsonl").read_bytes() == source_canonical
    await case.hard_restart()
    history = await case.client.post(f"/api/v1/sessions/{target}/history", json={"direction": "head", "turns": 10})
    assert history.status_code == 200, history.text
    restored, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    assert restored["file_id"] == original["file_id"]
    assert restored["directory_uri"] == original["directory_uri"]
    assert restored["availability"] == "available"
    target_calls = await case.plan(target, [
        _tool("read_file", {"path": original["file_id"]}, "cold-original"),
        _tool("read_file", {"path": derived}, "cold-derived"),
        {"content": "重启后原件与处理结果均可用"},
    ])
    response = await case.client.post(f"/api/v1/sessions/{target}/messages", json={
        "message": {"content": "从 fork 冷恢复读取同一原件和派生结果"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    # 冷读使用真实新执行取得 capability，不能由测试替目标补建执行或关联。
    for call, name, expected in zip(target_calls[:2], ("cold-original", "cold-derived"),
                                    ("original-for-product-flow", "original-for-product-flow processed"), strict=True):
        result = case.tool_result(target, call + ":tool-call:" + name)
        assert result["tool_outcome"] == "success" and result["execution_confirmed"] is True, result
        assert expected in result["content"], result
    assert (case.main_thread_node(session) / "rollout/rollout.jsonl").read_bytes() == source_canonical
    assert (case.main_thread_node(target) / "rollout/rollout.jsonl").read_bytes().startswith(source_canonical)
    case.record("attachment-product-flow", {
        "source_session": session, "fork": fork, "attachment": original,
        "source_calls": calls, "target_calls": target_calls,
        "shell_result": shell, "read_result": read,
    })
