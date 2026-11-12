"""可选 Codex TUI MCP 经 Gateway 插件桥和 Backend HTTP 调用。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tomllib
import zipfile
from pathlib import Path
from typing import cast

import pytest

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _plugin,
    _publish,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 使用真实 Gateway/Backend 产品装具。
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_tool_restrictions as mcp_plugin_tool_restrictions,  # noqa: PLC0414 - mcp_plugin_case 的 fixture 依赖
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.plugin_package import install_plugin_async
from tests.support.trace import get_trace_payload


def _required_tool_call(logs: list[dict[str, object]], *, name: str) -> dict[str, object]:
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
                if isinstance(tool_call, dict) and tool_call.get("name") == name:
                    return tool_call
    raise AssertionError(f"模型请求记录没有调用预期工具: {name}")


def _codex_server_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        pytest.skip(f"Codex TUI MCP 配置不存在，跳过可选产品验收: {path}")
    with path.open("rb") as stream:
        raw_config = tomllib.load(stream)
    raw_servers = raw_config.get("mcp_servers")
    if not isinstance(raw_servers, dict):
        pytest.skip("Codex 配置没有 mcp_servers，跳过可选产品验收")
    raw_server = raw_servers.get("tui-mcp")
    if not isinstance(raw_server, dict):
        pytest.skip("Codex 未配置 tui-mcp，跳过可选产品验收")

    command = raw_server.get("command")
    args = raw_server.get("args", [])
    if not isinstance(command, str) or not command.strip():
        pytest.skip("Codex tui-mcp 缺少 command，跳过可选产品验收")
    resolved_command = shutil.which(command)
    if resolved_command is None:
        pytest.skip("Codex tui-mcp 命令不在 PATH 或不可执行，跳过可选产品验收")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        pytest.skip("Codex tui-mcp args 不是字符串数组，跳过可选产品验收")

    raw_env = raw_server.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and key and "=" not in key and "\0" not in key
        and isinstance(value, str) and "\0" not in value
        for key, value in raw_env.items()
    ):
        pytest.skip("Codex tui-mcp env 不是有效的字符串字典，跳过可选产品验收")
    raw_cwd = raw_server.get("cwd")
    if raw_cwd is not None and (not isinstance(raw_cwd, str) or not raw_cwd):
        pytest.skip("Codex tui-mcp cwd 不是有效路径，跳过可选产品验收")
    cwd = Path(raw_cwd).expanduser() if isinstance(raw_cwd, str) else Path.cwd()
    cwd = cwd.resolve()
    if not cwd.is_dir():
        pytest.skip("Codex tui-mcp cwd 不存在，跳过可选产品验收")

    return {
        "command": resolved_command,
        "args": list(args),
        "cwd": str(cwd),
        "env": dict(raw_env),
    }


@pytest.fixture(autouse=True)
def codex_tui_mcp_parameters(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    server = _codex_server_config(codex_home / "config.toml")
    values: list[str] = []

    def private_reference(value: str) -> str:
        name = f"BOXTEAM_CODEX_TUI_PARAMETER_{len(values)}"
        values.append(value)
        monkeypatch.setenv(name, value)
        return "${" + name + "}"

    raw_args = cast(list[str], server["args"])
    raw_env = cast(dict[str, str], server["env"])
    parameters = {
        "command": private_reference(cast(str, server["command"])),
        "args": [private_reference(item) for item in raw_args],
        "cwd": private_reference(cast(str, server["cwd"])),
        "env": {key: private_reference(value) for key, value in raw_env.items()},
    }
    return parameters


def _package_with_private_launch_parameters(prepared: Path, output: Path) -> Path:
    with zipfile.ZipFile(prepared) as source:
        manifest = cast(dict[str, object], json.loads(source.read("plugin.jsonc")))
        plugins = manifest.get("plugins")
        if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
            raise ValueError("MCP stdio 插件包必须有唯一插件声明")
        plugin = cast(dict[str, object], plugins[0])
        contributions = cast(dict[str, object], plugin["contributes"])
        configuration = cast(dict[str, object], contributions["configuration"])
        properties = cast(dict[str, object], configuration["properties"])
        cast(dict[str, object], properties["command"])["writeOnly"] = True
        cast(dict[str, object], properties["args"]["items"])["writeOnly"] = True
        cast(dict[str, object], properties["cwd"])["writeOnly"] = True
        cast(dict[str, object], properties["env"]["additionalProperties"])["writeOnly"] = True
        with zipfile.ZipFile(output, "w") as archive:
            for entry in source.infolist():
                contents = (
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                    if entry.filename == "plugin.jsonc"
                    else source.read(entry)
                )
                archive.writestr(entry, contents)
    return output


@pytest.mark.asyncio
async def test_agent_uses_codex_tui_mcp_through_gateway_plugin(
    mcp_plugin_case: McpPluginCase,
    prepared_mcp_stdio_package: Path,
    codex_tui_mcp_parameters: dict[str, object],
) -> None:
    case = mcp_plugin_case
    archive = _package_with_private_launch_parameters(
        prepared_mcp_stdio_package,
        case.context.artifacts_dir / "codex-tui-mcp-bridge.zip",
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    await install_plugin_async(
        lambda path, payload: case.client.post(path, json=payload),
        gateway_id=case.gateway_id,
        path=str(archive),
        expected_digest=digest,
    )
    service = {
        "gateway_id": case.gateway_id,
        "workspace_id": case.workspace_id,
        "package_id": "boxteam.mcp-stdio",
        "package_version": "0.1.0",
        "plugin_id": "stdio",
        "service_id": "mcp",
    }
    await _publish(case, {
        "plugins": {
            "runtime": {"entries": [{
                "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
                "plugin_id": "stdio", "enabled": True,
            }]},
            "configuration": {"values": {"boxteam.mcp-stdio": {
                "stdio": codex_tui_mcp_parameters,
            }}},
        },
        "mcp": {"servers": {"tui-mcp": {
            "enabled": True,
            "transport": "plugin",
            "service": service,
        }}},
    }, "codex-tui-mcp-plugin-enable")
    plugin = await _plugin(case, state="ready")
    assert plugin["instance"] is not None

    async with asyncio.timeout(60):
        while True:
            response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
            response.raise_for_status()
            servers = response.json()["data"]
            server = next((item for item in servers if item["server_id"] == "tui-mcp"), None)
            if server is not None and server["status"] == "ready":
                break
            await asyncio.sleep(0.1)

    assert server["transport"] == "streamable_http"
    assert server["service"] == service
    tool = next((item for item in server["tools"] if item["remote_name"] == "list_sessions"), None)
    if tool is None:
        pytest.skip("Gateway 已接通真实 Codex tui-mcp，但它未暴露 list_sessions；Agent 调用未验")
    tool_id = tool["tool_id"]
    assert tool_id == "mcp__tui-mcp__list_sessions"

    session = await create_session(case.client, "Codex tui-mcp Gateway 插件")
    response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    response.raise_for_status()
    thread = response.json()["data"]["thread_id"]
    tool_calls = [
        {
            "id": "codex-tui-search",
            "type": "function",
            "function": {
                "name": "search_tools",
                "arguments": json.dumps({"tool_ids": [tool_id]}),
            },
        },
        {
            "id": "codex-tui-list-sessions",
            "type": "function",
            "function": {
                "name": "invoke_extension_tool",
                "arguments": json.dumps({"tool_name": tool_id, "arguments": {}}),
            },
        },
    ]
    deltas = [
        {"tool_calls": [tool_calls[0]]},
        {"tool_calls": [tool_calls[1]]},
        {"content": "已通过 Codex TUI MCP 读取会话列表。"},
    ]
    interactions: list[dict[str, object]] = []
    for index, delta in enumerate(deltas):
        cassette = build_cassette(
            asset_id=f"codex-tui-mcp-{index}",
            model="mcp-plugin-model",
            url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-codex-tui-mcp-{index}",
                model="mcp-plugin-model",
                deltas=({"role": "assistant", **delta},),
                finish_reason="tool_calls" if "tool_calls" in delta else "stop",
            ),
        )
        interactions_by_call = cast(
            dict[str, list[dict[str, object]]], cassette.raw,
        )
        interactions.append(interactions_by_call["interactions"][0])
    case.replay.control.plan_model_calls(session, thread, interactions)

    request_id = "codex-tui-mcp-agent-call"
    response = await case.client.post(
        f"/api/v1/sessions/{session}/messages",
        json={
            "message": {"content": (
                "必须通过 invoke_extension_tool 调用 mcp__tui-mcp__list_sessions，"
                "arguments={}；调用完成后说明已经读取会话列表。"
            )},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
        headers={"X-Request-ID": request_id},
    )
    response.raise_for_status()
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["request_id"] == request_id
    admission = response.json()["data"]
    await wait_for_job_done(case.client, admission["job_id"], max_attempts=120)

    logs_response = await case.client.get(f"/api/v1/sessions/{session}/llm-request-logs")
    logs_response.raise_for_status()
    invocation = _required_tool_call(logs_response.json()["data"], name="invoke_extension_tool")
    invocation_arguments = json.loads(invocation["arguments"])
    assert invocation_arguments == {"tool_name": tool_id, "arguments": {}}
    traces_response = await case.client.get(f"/api/v1/sessions/{session}/traces")
    traces_response.raise_for_status()
    events = [
        item for item in traces_response.json()["data"]["items"]
        if item["type"] in {"tool_call_start", "tool_call_end"}
        and get_trace_payload(item).get("tool_name") == "invoke_extension_tool"
    ]
    assert [item["type"] for item in events] == ["tool_call_start", "tool_call_end"]
    with RolloutCheckpointSaver(case.workspace_root / ".boxteam/sessions") as checkpoint:
        items = checkpoint._storage.storage.read_items(session, thread)
        results = [
            item for item in items
            if item.semantic_kind == "tool_result"
            and item.tool_call_id is not None
            and item.tool_call_id.endswith("codex-tui-list-sessions")
        ]
        assert len(results) == 1 and isinstance(results[0].payload, dict)
        assert results[0].payload["tool_outcome"] == "success"

    evidence = {
        "gateway_id": case.gateway_id,
        "workspace_id": case.workspace_id,
        "plugin_instance_id": plugin["instance"]["instance_id"],
        "connection_ref": server["connection_ref"],
        "tool_id": tool_id,
        "request_id": request_id,
        "tool_outcome": "success",
        "user_result_values_persisted": False,
    }
    (case.context.artifacts_dir / "codex-tui-mcp.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
