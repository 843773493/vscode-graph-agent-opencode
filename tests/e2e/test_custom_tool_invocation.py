from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from tests.e2e.utils import (
    get_trace_payload,
    last_assistant_message,
    wait_for_job_done,
)

CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS = (
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
    template_root = project_root / "asset" / "custom_tool_test_workspace"
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

    for relative_item in CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS:
        item = template_root / relative_item
        if not item.exists():
            raise FileNotFoundError(f"custom tool e2e 模板缺少必要文件: {item}")
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
        elif isinstance(tool_def, str) and CUSTOM_TOOL_INVOKER_NAME in tool_def:
            names.add(CUSTOM_TOOL_INVOKER_NAME)
    return names


def _custom_tool_targets_from_llm_log(log_record: dict[str, Any]) -> set[str]:
    response_items = log_record.get("response", {}).get("result") or []
    targets: set[str] = set()
    if not isinstance(response_items, list):
        return targets
    for item in response_items:
        if not isinstance(item, dict):
            continue
        for tool_call in item.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            if tool_call.get("name") != CUSTOM_TOOL_INVOKER_NAME:
                continue
            args = tool_call.get("args")
            if isinstance(args, dict) and isinstance(args.get("tool_name"), str):
                targets.add(args["tool_name"])
    return targets


def _system_message_text_from_llm_log(log_record: dict[str, Any]) -> str:
    system_message = log_record.get("request", {}).get("system_message")
    if isinstance(system_message, str):
        return system_message
    if not isinstance(system_message, dict):
        return ""
    content = system_message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _read_file_path_from_trace(trace: dict[str, Any]) -> str:
    args = get_trace_payload(trace).get("args", {})
    if not isinstance(args, dict):
        return ""
    value = args.get("file_path") or args.get("path")
    return str(value or "")


def _json_object_from_text(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AssertionError(f"未在文本中找到 JSON object: {text!r}")
    parsed = json.loads(text[start:end + 1])
    assert isinstance(parsed, dict)
    return parsed


async def _write_source_session_checkpoint(
    *,
    workspace_root: str,
    session_id: str,
    source_marker: str,
) -> None:
    saver = FileSystemCheckpointSaver(
        base_dir=Path(workspace_root) / ".boxteam" / "checkpoints"
    )
    messages = [
        HumanMessage(
            content=f"请只回复：{source_marker}",
            response_metadata={"message_id": "msg_source_user"},
        ),
        AIMessage(
            content=[{"type": "text", "text": source_marker}],
            response_metadata={"message_id": "msg_source_assistant"},
        ),
    ]
    checkpoint = {
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-source-session",
    }
    await saver.aput(
        build_checkpoint_config(session_id),
        checkpoint,
        {"source": "e2e_fixture", "step": 1, "writes": {}},
        {"messages": 1},
    )


@pytest.mark.asyncio
async def test_workspace_agents_doc_uses_stable_custom_tool_invoker_and_frontend_views(
    client: httpx.AsyncClient,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Skill Custom Tool E2E"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    prompt = (
        "请先读取当前工作区 AGENTS.md 里的扩展工具说明。"
        "当你看到用户要求执行 test_tool_2 时，必须根据 AGENTS.md 找到并读取正确的 skill。"
        "然后必须按该 skill 发起真实工具调用来执行 test_tool_2，不要只描述调用计划。"
        "最终回复只能是该扩展工具返回文本本身。"
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
    assert messages[0]["role"] == "user"
    assert messages[-1]["role"] == "assistant"
    for message in messages[1:-1]:
        if message["role"] == "user":
            assert "<system_reminder>" in message["content"]
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
    custom_tool_start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert custom_tool_start_payloads
    assert custom_tool_start_payloads[-1].get("invocation_tool_name") == CUSTOM_TOOL_INVOKER_NAME
    custom_tool_start_dtos = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert custom_tool_start_dtos[-1].get("skill_names", []) == ["test-tool-2"]

    logs_response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    assert len(logs) >= 2
    assert any(
        "asset/custom_tool_test_workspace/` 是扩展工具 e2e 测试使用的工作区模板"
        in _system_message_text_from_llm_log(log)
        for log in logs
    )
    assert any(
        "<workspace_agents_md path=\"/AGENTS.md\">" in _system_message_text_from_llm_log(log)
        for log in logs
    )
    for log in logs:
        tool_names = _tool_names_from_llm_log(log)
        assert CUSTOM_TOOL_INVOKER_NAME in tool_names
        assert "test_tool_2" not in tool_names
    assert any(
        "test_tool_2" in _custom_tool_targets_from_llm_log(log)
        for log in logs
    )

    agent_state_response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert agent_state_response.status_code == 200
    agent_state = agent_state_response.json()["data"]
    assert "test_tool_2" in agent_state["jsonl"]
    assert "4568" in agent_state["jsonl"]


@pytest.mark.asyncio
async def test_custom_tool_reads_recent_text_messages_from_another_session(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    source_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Source Session For History Tool"},
    )
    assert source_session_response.status_code == 200
    source_session_id = source_session_response.json()["data"]["session_id"]

    source_marker = "SOURCE_SESSION_HISTORY_ALPHA"
    await _write_source_session_checkpoint(
        workspace_root=e2e_workspace_root_path,
        session_id=source_session_id,
        source_marker=source_marker,
    )

    source_agent_state_response = await client.get(
        f"/api/v1/sessions/{source_session_id}/agent-state/messages"
    )
    assert source_agent_state_response.status_code == 200
    source_agent_state_jsonl = source_agent_state_response.json()["data"]["jsonl"]
    assert source_marker in source_agent_state_jsonl

    reader_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Reader Session For History Tool"},
    )
    assert reader_session_response.status_code == 200
    reader_session_id = reader_session_response.json()["data"]["session_id"]

    prompt = (
        "请先读取当前工作区 AGENTS.md 里的扩展工具说明。"
        "当你看到用户要求读取另一个会话最近用户消息和模型文本消息时，"
        "必须根据 AGENTS.md 找到并读取正确的 skill。"
        f"然后使用默认 rounds 调用 read_session_recent_text_messages，读取 session_id={source_session_id}。"
        "最终回复只能是该工具返回的 JSON 字符串，不要添加解释。"
    )
    reader_message_response = await client.post(
        f"/api/v1/sessions/{reader_session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert reader_message_response.status_code == 200
    reader_job_id = reader_message_response.json()["data"]["job_id"]
    reader_job_data = await wait_for_job_done(client, reader_job_id, max_attempts=120)
    assert reader_job_data["status"] in {"completed", "succeeded"}

    messages_response = await client.get(f"/api/v1/sessions/{reader_session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    result = _json_object_from_text(last_assistant_message(messages))
    assert result["session_id"] == source_session_id
    assert result["rounds"] == 5
    assert result["user_message_count"] >= 1
    assert any(
        item.get("role") == "user" and source_marker in item.get("text", "")
        for item in result["messages"]
    )
    assert any(
        item.get("role") == "assistant"
        and item.get("type") == "text"
        and source_marker in item.get("text", "")
        for item in result["messages"]
    )

    traces_response = await client.get(f"/api/v1/sessions/{reader_session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    read_file_paths = [
        _read_file_path_from_trace(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "read_file"
    ]
    assert any(
        path.endswith("/.boxteam/skills/read-session-recent-text-messages/SKILL.md")
        for path in read_file_paths
    )
    custom_tool_start_dtos = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "read_session_recent_text_messages"
    ]
    assert custom_tool_start_dtos
    assert custom_tool_start_dtos[-1].get("skill_names", []) == [
        "read-session-recent-text-messages"
    ]
    assert (
        get_trace_payload(custom_tool_start_dtos[-1]).get("invocation_tool_name")
        == CUSTOM_TOOL_INVOKER_NAME
    )

    logs_response = await client.get(f"/api/v1/sessions/{reader_session_id}/llm-request-logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    assert any(
        "read_session_recent_text_messages" in _custom_tool_targets_from_llm_log(log)
        for log in logs
    )

    reader_agent_state_response = await client.get(
        f"/api/v1/sessions/{reader_session_id}/agent-state/messages"
    )
    assert reader_agent_state_response.status_code == 200
    reader_agent_state_jsonl = reader_agent_state_response.json()["data"]["jsonl"]
    assert "empty_response_retry" not in reader_agent_state_jsonl
    assert "<system_reminder>" not in reader_agent_state_jsonl
