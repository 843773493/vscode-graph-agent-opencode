from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tools.testing import (
    LARGE_TEST_OUTPUT,
    LARGE_TEST_TARGET_LINE_INDEX,
    LARGE_TEST_TARGET_VALUE,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    start_terminal_backend,
    terminal_ports,
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.processes import (
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.trace import get_trace_payload
from tests.support.workspaces import prepare_test_workspace

CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS = (
    "AGENTS.md",
    ".boxteam/skills",
)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = (
        project_root / "out" / "tests" / "e2e" / relative_test_path / "workspace"
    )
    template_root = project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace"
    prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS,
        shared_skill_root=project_root / "resources" / "skills",
    )
    return str(workspace_root)


@pytest.fixture(scope="module")
def e2e_terminal_backend(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> Generator[subprocess.Popen[str], None, None]:
    terminal_port, frontend_port = terminal_ports(e2e_backend_port)
    process = start_terminal_backend(
        backend_port=terminal_port,
        frontend_port=frontend_port,
        workspace_root=e2e_workspace_root_path,
    )
    try:
        yield process
    finally:
        terminate_process(process)


@pytest.fixture(scope="module")
def e2e_backend_process(
    e2e_terminal_backend: subprocess.Popen[str],
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
    is_debug: bool,
    e2e_model_stream_runtime_config_path: str | None,
) -> Generator[subprocess.Popen[str], None, None]:
    del e2e_terminal_backend
    debugpy_port = (
        int(os.getenv("BOXTEAM_E2E_BACKEND_DEBUGPY_PORT")) if is_debug else None
    )
    env_overrides = {
        "BOXTEAM_TERMINAL_BACKEND_URL": (
            f"http://127.0.0.1:{terminal_ports(e2e_backend_port)[0]}"
        ),
    }
    if e2e_model_stream_runtime_config_path is not None:
        env_overrides["BOXTEAM_TEST_MODEL_STREAM_CONFIG"] = (
            e2e_model_stream_runtime_config_path
        )
    handle = start_backend_process(
        workspace_root=e2e_workspace_root_path,
        port=e2e_backend_port,
        log_name="e2e-backend",
        debugpy_port=debugpy_port,
        env_overrides=env_overrides,
        env_unset=("BOXTEAM_TEST_MODEL_STREAM_CONFIG",),
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


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
        elif isinstance(tool_def, str) and EXTENSION_TOOL_INVOKER_NAME in tool_def:
            names.add(EXTENSION_TOOL_INVOKER_NAME)
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
            if tool_call.get("name") != EXTENSION_TOOL_INVOKER_NAME:
                continue
            args = tool_call.get("args")
            if isinstance(args, dict) and isinstance(args.get("tool_name"), str):
                targets.add(args["tool_name"])
    return targets


def _system_message_text_from_llm_log(log_record: dict[str, Any]) -> str:
    request = log_record.get("request", {})
    system_message = request.get("system_message")
    if system_message is None:
        messages = request.get("messages")
        if isinstance(messages, list):
            system_message = next(
                (
                    message
                    for message in messages
                    if isinstance(message, dict) and message.get("type") == "system"
                ),
                None,
            )
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


def _json_object_from_text(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AssertionError(f"未在文本中找到 JSON object: {text!r}")
    parsed = json.loads(text[start:end + 1])
    assert isinstance(parsed, dict)
    return parsed


async def _list_all_session_traces(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """按诊断页游标读取完整轨迹，避免长 reasoning 把工具事件挤出尾页。"""
    pages: list[list[dict[str, Any]]] = []
    cursor: str | None = None
    for _ in range(100):
        params: dict[str, object] = {"limit": 200}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            f"/api/v1/sessions/{session_id}/traces",
            params=params,
        )
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        items = page.get("items")
        assert isinstance(items, list)
        pages.append([item for item in items if isinstance(item, dict)])
        if not page.get("has_more"):
            return [item for page in reversed(pages) for item in page]
        next_cursor = page.get("next_cursor")
        assert isinstance(next_cursor, str) and next_cursor
        cursor = next_cursor
    raise AssertionError(f"读取 session trace 超过分页上限: session_id={session_id}")


async def _write_source_session_checkpoint(
    *,
    workspace_root: str,
    session_id: str,
    source_marker: str,
) -> None:
    saver = RolloutCheckpointSaver(
        sessions_dir=Path(workspace_root) / ".boxteam" / "sessions"
    )
    messages = [
        HumanMessage(
            content="请只回复源会话中的标记文本。",
            response_metadata={"message_id": "msg_source_user"},
        ),
        AIMessage(
            content=[{"type": "text", "text": source_marker}],
            response_metadata={"message_id": "msg_source_assistant"},
        ),
    ]
    checkpoint = {
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
        "id": "ckpt-source-session",
    }
    await saver.aput(
        build_checkpoint_config(session_id),
        checkpoint,
        {"source": "e2e_fixture", "step": 1, "writes": {}},
        {"messages": "1"},
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
        "请使用 skill_load(name=test-tool-2) 加载执行 test_tool_2 所需的 skill。"
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
            assert (
                "<system_reminder>" in message["content"]
                or "上下文 Skill `test-tool-2` 已按 activation 注入。" in message["content"]
            )
    assert "4568" in last_assistant_message(messages)

    traces = await _list_all_session_traces(client, session_id)
    tool_starts = [
        get_trace_payload(trace).get("tool_name")
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    assert "skill_load" in tool_starts
    assert "test_tool_2" in tool_starts
    skill_load_start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "skill_load"
    ]
    assert len(skill_load_start_payloads) == 1
    assert skill_load_start_payloads[0].get("args", {}).get("name") == "test-tool-2"
    custom_tool_start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert custom_tool_start_payloads
    assert custom_tool_start_payloads[-1].get("invocation_tool_name") == EXTENSION_TOOL_INVOKER_NAME
    custom_tool_start_dtos = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert custom_tool_start_dtos[-1].get("skill_names", []) == ["test-tool-2"]
    custom_tool_end_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") == "test_tool_2"
    ]
    assert custom_tool_end_payloads[-1]["result"] == "4568"
    assert custom_tool_end_payloads[-1].get("tool_output") is None

    logs_response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    assert len(logs) >= 2
    for log in logs:
        tool_names = _tool_names_from_llm_log(log)
        assert EXTENSION_TOOL_INVOKER_NAME in tool_names
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
async def test_large_custom_tool_output_is_persisted_and_bounded_for_model(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Large Tool Output E2E"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "请使用 skill_load(name=large-test-output) 加载 large_test_output 对应的 skill，"
                    "按说明真实调用 large_test_output。目标值不在工具返回的头尾预览中，"
                    "你必须继续使用 grep 和 read_file 从完整文件中找到它，"
                    "最后严格按 skill 要求回复。"
                )
            },
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]
    job_data = await wait_for_job_done(client, job_id, max_attempts=120)
    assert job_data["status"] in {"completed", "succeeded"}

    traces = await _list_all_session_traces(client, session_id)
    tool_end_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") == "large_test_output"
    ]
    assert tool_end_payloads
    payload = tool_end_payloads[-1]
    result = payload["result"]
    assert isinstance(result, str)
    assert "工具输出过大" in result
    assert "large-output-line-0000" in result
    assert "large-output-line-2399" in result
    assert LARGE_TEST_TARGET_VALUE not in result
    assert len(result.encode("utf-8")) <= 50 * 1024

    reference = payload.get("tool_output")
    assert isinstance(reference, dict)
    assert reference["type"] == "tool_output"
    assert reference["tool_name"] == "large_test_output"
    artifact_uri = str(reference["path"])
    artifact_prefix = f"boxteam-session://{session_id}/tool-results/"
    assert artifact_uri.startswith(artifact_prefix)
    output_name = artifact_uri.removeprefix(artifact_prefix)
    assert reference["read_path"] == (
        f"session-artifacts/{session_id}/tool-results/{output_name}"
    )
    assert reference["byte_count"] == len(LARGE_TEST_OUTPUT.encode("utf-8"))
    assert reference["line_count"] == 2_400
    assert reference["content_sha256"] == hashlib.sha256(
        LARGE_TEST_OUTPUT.encode("utf-8")
    ).hexdigest()
    session_root = get_session_path_resolver(
        Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    output_path = session_root / "tool-results" / output_name
    assert output_path.parent == session_root / "tool-results"
    assert output_path.read_text(encoding="utf-8") == LARGE_TEST_OUTPUT
    tool_start_payloads = [
        get_trace_payload(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    skill_load_index = next(
        index
        for index, item in enumerate(tool_start_payloads)
        if item.get("tool_name") == "skill_load"
    )
    large_call_index = next(
        index
        for index, item in enumerate(tool_start_payloads)
        if item.get("tool_name") == "large_test_output"
    )
    assert skill_load_index < large_call_index
    grep_call_index = next(
        index
        for index, item in enumerate(tool_start_payloads)
        if index > large_call_index and item.get("tool_name") == "grep"
    )
    read_call_index = next(
        index
        for index, item in enumerate(tool_start_payloads)
        if index > grep_call_index and item.get("tool_name") == "read_file"
    )
    grep_args = tool_start_payloads[grep_call_index].get("args")
    assert isinstance(grep_args, dict)
    assert grep_args.get("pattern") == "retrieval-target"
    assert grep_args.get("path") in {
        reference["read_path"],
        str(Path(reference["read_path"]).parent),
    }
    assert grep_args.get("output_mode") == "content"
    read_args = tool_start_payloads[read_call_index].get("args")
    assert isinstance(read_args, dict)
    assert read_args.get("path") == reference["read_path"]
    line_offset = read_args.get("line_offset", 1)
    read_limit = read_args.get("max_lines", 2_000)
    assert isinstance(line_offset, int)
    assert isinstance(read_limit, int)
    read_start = line_offset - 1
    assert read_start <= LARGE_TEST_TARGET_LINE_INDEX < read_start + read_limit

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    tool_messages = [
        message
        for log in logs
        for message in log.get("request", {}).get("messages", [])
        if isinstance(message, dict) and message.get("type") == "tool"
    ]
    assert any(
        isinstance(message.get("content"), str)
        and "工具输出过大" in message["content"]
        for message in tool_messages
    )
    assert any(
        isinstance(message.get("content"), str)
        and "工具输出过大" in message["content"]
        and LARGE_TEST_TARGET_VALUE not in message["content"]
        for message in tool_messages
    )
    assert all(
        not isinstance(message.get("content"), str)
        or len(message["content"].encode("utf-8")) <= 50 * 1024
        for message in tool_messages
    )

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assert LARGE_TEST_TARGET_VALUE in last_assistant_message(messages)


@pytest.mark.asyncio
async def test_custom_tool_reads_searches_and_expands_another_session_context(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    source_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Source Session For History Tool"},
    )
    assert source_session_response.status_code == 200
    source_session_data = source_session_response.json()["data"]
    source_session_id = source_session_data["session_id"]
    source_resource = f"boxteam://session/{source_session_id}"

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
        "先调用 skill_load，参数只能是 name=gateway-context；它不计入下面的业务步骤。"
        "随后严格完成以下三个业务调用，不要改写资源地址，也不要使用 boxteam://gateway："
        f"第一步调用 read_context，resource 原样使用 {source_resource}，view=overview。"
        "第二步调用 search_context，参数名必须使用 query（不是 pattern），"
        "resource 仍原样使用同一个地址，"
        f"query 原样使用 {source_marker}；将第一步响应的 revision 原样复制到 "
        "第二步的 expected_revision（请求中禁止传 revision 字段）。"
        "第三步调用 read_context；resource 使用第二步响应 matches[0].locator，"
        "view=records，并将 matches[0].revision 原样复制到 expected_revision。"
        "三个业务调用全部成功后才回复完成，不要抄写工具返回的大段 JSON。"
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

    traces = await _list_all_session_traces(client, reader_session_id)
    context_tool_results = [
        (
            str(get_trace_payload(trace).get("tool_name")),
            json.loads(str(get_trace_payload(trace)["result"])),
        )
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name")
        in {"read_context", "search_context"}
    ]
    read_results = [
        result for tool_name, result in context_tool_results if tool_name == "read_context"
    ]
    search_results = [
        result
        for tool_name, result in context_tool_results
        if tool_name == "search_context"
    ]
    assert len(read_results) >= 2
    assert search_results
    overview_result = read_results[0]
    search_result = search_results[0]
    expanded_result = read_results[-1]
    assert overview_result["view"] == "overview"
    assert overview_result["revision"]
    assert source_marker in json.dumps(overview_result, ensure_ascii=False)
    assert search_result["revision"] == overview_result["revision"]
    assert search_result["total_matches"] >= 1
    assert search_result["matches"][0]["locator"]
    assert expanded_result["revision"] == search_result["matches"][0]["revision"]
    assert source_marker in json.dumps(expanded_result, ensure_ascii=False)

    skill_load_paths = [
        get_trace_payload(trace).get("args", {}).get("name")
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "skill_load"
    ]
    assert skill_load_paths == ["gateway-context"]
    for custom_tool_name in ("read_context", "search_context"):
        custom_tool_start_dtos = [
            trace
            for trace in traces
            if trace.get("type") == "tool_call_start"
            and get_trace_payload(trace).get("tool_name") == custom_tool_name
        ]
        assert custom_tool_start_dtos
        assert custom_tool_start_dtos[-1].get("skill_names", []) == [
            "gateway-context"
        ]
        assert (
            get_trace_payload(custom_tool_start_dtos[-1]).get("invocation_tool_name")
            == EXTENSION_TOOL_INVOKER_NAME
        )

    logs_response = await client.get(f"/api/v1/sessions/{reader_session_id}/llm-request-logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    invoked_custom_tools = set().union(
        *[_custom_tool_targets_from_llm_log(log) for log in logs]
    )
    assert {"read_context", "search_context"} <= invoked_custom_tools

    reader_agent_state_response = await client.get(
        f"/api/v1/sessions/{reader_session_id}/agent-state/messages"
    )
    assert reader_agent_state_response.status_code == 200
    reader_agent_state_jsonl = reader_agent_state_response.json()["data"]["jsonl"]
    assert "empty_response_retry" not in reader_agent_state_jsonl
    assert "<system_reminder>" not in reader_agent_state_jsonl
