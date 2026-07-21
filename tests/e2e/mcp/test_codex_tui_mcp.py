from __future__ import annotations

import asyncio
import json
import os
import shutil
import tomllib
from pathlib import Path

import httpx
import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection

from tests.e2e.mcp.configuration import write_e2e_mcp_config
from tests.e2e.utils import get_trace_payload, last_assistant_message, wait_for_job_done


def _read_codex_tui_mcp_config() -> dict[str, object]:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        pytest.skip(f"Codex 配置不存在，跳过 tui-mcp E2E: {config_path}")

    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    raw_servers = config.get("mcp_servers")
    if not isinstance(raw_servers, dict):
        pytest.skip("Codex 配置没有 mcp_servers，跳过 tui-mcp E2E")
    raw_tui_server = raw_servers.get("tui-mcp")
    if not isinstance(raw_tui_server, dict):
        pytest.skip("Codex 当前未安装 tui-mcp，跳过对应 E2E")

    command = raw_tui_server.get("command")
    args = raw_tui_server.get("args", [])
    if not isinstance(command, str) or not command.strip():
        pytest.skip("Codex tui-mcp 配置缺少 command，跳过对应 E2E")
    if shutil.which(command) is None:
        pytest.skip(f"Codex tui-mcp 命令不可用，跳过对应 E2E: {command}")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        pytest.skip("Codex tui-mcp args 不是字符串数组，跳过对应 E2E")

    server_config: dict[str, object] = {
        "enabled": True,
        "transport": "stdio",
        "command": command,
        "args": list(args),
    }
    raw_env = raw_tui_server.get("env")
    if isinstance(raw_env, dict) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_env.items()
    ):
        server_config["env"] = dict(raw_env)
    raw_cwd = raw_tui_server.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd:
        server_config["cwd"] = raw_cwd
    return server_config


async def _probe_tui_mcp(server_config: dict[str, object]) -> None:
    connection: Connection = {
        "transport": "stdio",
        "command": str(server_config["command"]),
        "args": list(server_config["args"]),
        **(
            {"env": dict(server_config["env"])}
            if isinstance(server_config.get("env"), dict)
            else {}
        ),
        **(
            {"cwd": str(server_config["cwd"])}
            if isinstance(server_config.get("cwd"), str)
            else {}
        ),
    }
    client = MultiServerMCPClient({"tui-mcp": connection})
    tools = await client.get_tools(server_name="tui-mcp")
    list_sessions_tool = next(
        (tool for tool in tools if tool.name == "list_sessions"),
        None,
    )
    if list_sessions_tool is None:
        pytest.skip("Codex tui-mcp 未暴露 list_sessions，跳过对应 E2E")
    await list_sessions_tool.ainvoke({})


@pytest.fixture(scope="module")
def codex_tui_mcp_server_config() -> dict[str, object]:
    server_config = _read_codex_tui_mcp_config()
    try:
        asyncio.run(_probe_tui_mcp(server_config))
    except Exception as error:
        pytest.skip(f"Codex tui-mcp 当前无法启动，跳过对应 E2E: {error}")
    return server_config


@pytest.fixture(scope="module")
def e2e_config_path(
    e2e_workspace_root_path: str,
    codex_tui_mcp_server_config: dict[str, object],
) -> str:
    return write_e2e_mcp_config(
        workspace_root=Path(e2e_workspace_root_path),
        servers={"tui-mcp": codex_tui_mcp_server_config},
        allowed_mcp_tools=["mcp__tui-mcp__list_sessions"],
    )


@pytest.mark.asyncio
async def test_agent_auto_detects_and_uses_codex_tui_mcp(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    servers_response = await client.get("/api/v1/mcp/servers")
    assert servers_response.status_code == 200
    servers = servers_response.json()["data"]
    tui_server = next(item for item in servers if item["server_id"] == "tui-mcp")
    assert tui_server["status"] == "ready"
    list_sessions_tool = next(
        item
        for item in tui_server["tools"]
        if item["remote_name"] == "list_sessions"
    )
    assert list_sessions_tool["tool_id"] == "mcp__tui-mcp__list_sessions"

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Codex tui-mcp E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "必须调用工具 mcp__tui-mcp__list_sessions 获取真实结果，"
                    "不得猜测或跳过工具调用；调用完成后简要说明结果。"
                )
            },
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    await wait_for_job_done(
        client,
        message_response.json()["data"]["job_id"],
        max_attempts=180,
    )
    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    assistant_message = last_assistant_message(
        messages_response.json()["data"]["items"]
    )
    assert assistant_message

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    model_tool_call = _find_model_tool_call(
        logs,
        tool_name="mcp__tui-mcp__list_sessions",
    )

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    tool_start = _find_tool_trace(
        traces,
        trace_type="tool_call_start",
        tool_name="mcp__tui-mcp__list_sessions",
    )
    tool_end = _find_tool_trace(
        traces,
        trace_type="tool_call_end",
        tool_name="mcp__tui-mcp__list_sessions",
    )

    evidence_path = (
        Path(e2e_workspace_root_path).parent
        / "artifacts"
        / "model-tool-call-evidence.json"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(
            {
                "tool_id": list_sessions_tool["tool_id"],
                "model_tool_call": model_tool_call,
                "tool_call_start": get_trace_payload(tool_start),
                "tool_call_end": get_trace_payload(tool_end),
                "assistant_message": assistant_message,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def _find_model_tool_call(
    logs: list[dict[str, object]],
    *,
    tool_name: str,
) -> dict[str, object]:
    for log in logs:
        response = log.get("response")
        if not isinstance(response, dict):
            continue
        result = response.get("result")
        if not isinstance(result, list):
            continue
        for item in result:
            if not isinstance(item, dict):
                continue
            tool_calls = item.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if isinstance(tool_call, dict) and tool_call.get("name") == tool_name:
                    return tool_call
    raise AssertionError(f"模型响应中没有调用预期工具: {tool_name}")


def _find_tool_trace(
    traces: list[dict[str, object]],
    *,
    trace_type: str,
    tool_name: str,
) -> dict[str, object]:
    for trace in traces:
        if trace.get("type") != trace_type:
            continue
        if get_trace_payload(trace).get("tool_name") == tool_name:
            return trace
    raise AssertionError(f"没有找到工具 Trace: type={trace_type} tool={tool_name}")
