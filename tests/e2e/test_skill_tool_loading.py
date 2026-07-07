from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.utils import (
    get_trace_payload,
    last_assistant_message,
    wait_for_job_done,
)

SKILL_WORKSPACE_TEMPLATE_ITEMS = (
    "AGENTS.md",
    ".boxteam/boxteam.json",
    ".boxteam/skills",
)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest, e2e_session_marker: str) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = project_root / "out" / "tests" / "e2e" / relative_test_path
    template_root = project_root / "asset" / "skill_test_workspace"
    lock_file = workspace_root / ".e2e_session_lock"

    same_session = lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == e2e_session_marker
    if workspace_root.exists() and not same_session:
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    for item in workspace_root.iterdir():
        if item.resolve() == lock_file.resolve():
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for relative_item in SKILL_WORKSPACE_TEMPLATE_ITEMS:
        item = template_root / relative_item
        if not item.exists():
            raise FileNotFoundError(f"skill e2e 模板缺少必要文件: {item}")
        target = workspace_root / item.name
        if relative_item.startswith(".boxteam/"):
            target = workspace_root / relative_item
            target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

    lock_file.write_text(e2e_session_marker, encoding="utf-8")
    return str(workspace_root)


def _tool_names_from_llm_log(log_record: dict[str, Any]) -> set[str]:
    tools = log_record.get("request", {}).get("tools") or []
    names: set[str] = set()
    for tool_def in tools:
        if isinstance(tool_def, dict):
            name = tool_def.get("name")
            if isinstance(name, str):
                names.add(name)
                continue
            function_def = tool_def.get("function")
            if isinstance(function_def, dict) and isinstance(function_def.get("name"), str):
                names.add(str(function_def["name"]))
        elif isinstance(tool_def, str) and "test_tool_2" in tool_def:
            names.add("test_tool_2")
    return names


def _request_json_text(log_record: dict[str, Any]) -> str:
    return str(log_record.get("request", {}))


@pytest.mark.asyncio
async def test_workspace_skill_loads_hidden_tool_and_frontend_consumed_views(
    client: httpx.AsyncClient,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Skill Hidden Tool E2E"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    prompt = (
        "请使用 test_tool_skill 技能完成任务。"
        "先按技能系统要求读取对应 SKILL.md，再严格按技能说明执行。"
        "最终回复只能是技能验证工具返回文本本身。"
    )
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]

    job_data = await wait_for_job_done(client, job_id, max_attempts=120)
    assert job_data["status"] in {"completed", "succeeded"}

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert last_assistant_message(messages).strip() == "4568"

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    tool_starts = [
        get_trace_payload(trace).get("tool_name")
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    assert "read_file" in tool_starts
    assert "test_tool_2" in tool_starts
    skill_tool_start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert skill_tool_start_payloads
    assert "test-tool-skill" in skill_tool_start_payloads[-1].get("skill_names", [])
    skill_tool_start_dtos = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert "test-tool-skill" in skill_tool_start_dtos[-1].get("skill_names", [])

    logs_response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    assert len(logs) >= 2
    assert "test_tool_2" not in _request_json_text(logs[0])
    assert "test_tool_2" not in _tool_names_from_llm_log(logs[0])
    assert any("test_tool_2" in _request_json_text(log) for log in logs[1:])
    assert any("test_tool_2" in _tool_names_from_llm_log(log) for log in logs[1:])

    agent_state_response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert agent_state_response.status_code == 200
    agent_state = agent_state_response.json()["data"]
    assert "test_tool_2" in agent_state["jsonl"]
    assert "4568" in agent_state["jsonl"]
