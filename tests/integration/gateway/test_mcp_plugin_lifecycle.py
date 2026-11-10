"""真实 Gateway 插件、stdio 桥、Backend 目录与 Agent 结果的完整纵向链。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import commentjson
import httpx
import psutil
import pytest

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.schema_server import (
    RESULT_AUDIO_BASE64,
    RESULT_IMAGE_BASE64,
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.mcp.http_endpoint import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 唯一真实 HTTP MCP 装具
)
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.plugin_package import install_plugin_async
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@dataclass(frozen=True)
class McpPluginCase:
    client: httpx.AsyncClient
    context: TestRunContext
    replay: ModelStreamReplay
    gateway_id: str
    workspace_id: str
    workspace_root: Path
    restart_gateway: Callable[[], None]


@pytest.fixture
async def mcp_plugin_case(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> AsyncIterator[McpPluginCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    gateway_url = f"http://127.0.0.1:{ports.port(21)}"
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config["llm"]["providers"] = [{
        "id": "mcp-plugin-replay", "endpoint": "https://opencode.ai/zen/v1",
        "model": "mcp-plugin-model", "api_key": "mcp-plugin-unused-key",
        "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "supports_reasoning": {"reasoning_content": False}, "model_info": {
            "supports_function_calling": True, "supports_vision": True,
            "supports_audio_input": True,
        }},
    }]
    for agent in config["agents"].values():
        agent["model"] = {"primary_provider": "mcp-plugin-replay", "fallback_providers": []}
        agent.setdefault("tools", {})["custom"] = []
    config["mcp"] = {"servers": {}}
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with model_stream_replay(
        context.artifacts_dir, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="mcp-plugin-lifecycle",
    ) as replay:
        def start_gateway():
            return start_gateway_process(
                workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
                port=ports.port(21), boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
                artifacts_dir=context.artifacts_dir / "gateway",
                extra_env={"BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(20)), **replay.environment},
                asgi_app="tests.support.e2e_control.gateway_app:app",
            )

        gateway = start_gateway()

        def restart_gateway() -> None:
            nonlocal gateway
            close_gateway_process(gateway)
            gateway = start_gateway()

        try:
            async with httpx.AsyncClient(
                base_url=gateway_url, headers=LOCAL_TOKEN_HEADERS, timeout=90, trust_env=False,
            ) as client:
                (await client.get("/api/gateway/users/current")).raise_for_status()
                response = await client.get("/api/gateway/workspaces")
                response.raise_for_status()
                active = response.json()["data"]["active_workspace"]
                gateway_id, workspace_id = active["gateway_id"], active["workspace_id"]
                client.headers.update({
                    "X-BoxTeam-Target-Gateway-Id": gateway_id,
                    "X-BoxTeam-Gateway-Id": gateway_id,
                    "X-BoxTeam-Workspace-Id": workspace_id,
                })
                yield McpPluginCase(
                    client, context, replay, gateway_id, workspace_id,
                    Path(integration_workspace_root_path), restart_gateway,
                )
        finally:
            close_gateway_process(gateway)


def _package(path: Path, prepared_package: Path) -> Path:
    path.write_bytes(prepared_package.read_bytes())
    return path


async def _publish(case: McpPluginCase, changes: dict[str, object], key: str) -> None:
    path = "/api/v1/config/sources/workspace_root_mutable_override"
    response = await case.client.get(path)
    response.raise_for_status()
    document = response.json()["data"]
    source = commentjson.loads(document["jsonc"])
    if "plugins" in changes:
        plugins = changes["plugins"]
        assert isinstance(plugins, dict)
        changes = {**changes, "plugins": {**source.get("plugins", {}), **plugins}}
    source.update(changes)
    response = await case.client.put(path, json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(source, ensure_ascii=False),
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": key,
    })
    response.raise_for_status()
    assert response.json()["data"]["edit"]["state"] == "active", response.text


async def _publish_expect_failure(case: McpPluginCase, changes: dict[str, object], key: str) -> dict:
    path = "/api/v1/config/sources/workspace_root_mutable_override"
    response = await case.client.get(path)
    response.raise_for_status()
    document = response.json()["data"]
    source = commentjson.loads(document["jsonc"])
    if "plugins" in changes:
        plugins = changes["plugins"]
        assert isinstance(plugins, dict)
        changes = {**changes, "plugins": {**source.get("plugins", {}), **plugins}}
    source.update(changes)
    response = await case.client.put(path, json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(source, ensure_ascii=False),
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": key,
    })
    assert response.status_code == 500, response.text
    result_document = await case.client.get(path, params={"idempotency_key": key})
    result_document.raise_for_status()
    result = result_document.json()["data"]["edit"]
    assert result["state"] == "recovery_required", result
    return result


async def _plugin(case: McpPluginCase, *, state: str | None = None) -> dict:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        response = await case.client.post("/api/gateway/service-plugins/status", json={"gateway_id": case.gateway_id})
        response.raise_for_status()
        snapshot = response.json()["data"]
        plugin = next((item for item in snapshot["plugins"]
                       if item["binding"]["package_id"] == "boxteam.mcp-stdio"
                       and item["binding"].get("workspace_id") == case.workspace_id), None)
        if plugin is not None and (state is None or plugin["state"] == state):
            return plugin
        assert plugin is None or plugin["state"] != "failed", plugin
        assert asyncio.get_running_loop().time() < deadline, snapshot
        await asyncio.sleep(0.1)


async def _catalog(case: McpPluginCase) -> dict:
    response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
    response.raise_for_status()
    assert response.json()["target"]["workspace_id"] == case.workspace_id
    server = next(item for item in response.json()["data"] if item["server_id"] == "probe")
    assert server["status"] == "ready", response.text
    assert server["transport"] == "streamable_http", response.text
    assert "provider_definition" not in server
    assert server["service"] == {
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
        "plugin_id": "stdio", "service_id": "mcp",
    }
    assert {tool["remote_name"] for tool in server["tools"]} == {"probe", "second"}
    return server


async def _agent_call(case: McpPluginCase, *, phase: str) -> list[dict]:
    client = case.client
    session = await create_session(client, f"MCP 插件 {phase}")
    response = await client.get(f"/api/v1/sessions/{session}/main-thread")
    response.raise_for_status()
    thread = response.json()["data"]["thread_id"]
    tool_calls = [{
        "id": f"mcp-plugin-{phase}-{index}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    } for index, (name, arguments) in enumerate((
        ("search_tools", {"tool_ids": ["mcp__probe__probe"]}),
        ("invoke_extension_tool", {"tool_name": "mcp__probe__probe", "arguments": {"result_mode": "success", "phase": phase, "call_mode": "progress"}}),
        ("invoke_extension_tool", {"tool_name": "mcp__probe__probe", "arguments": {"result_mode": "error", "phase": phase}}),
    ))]
    deltas = [{"tool_calls": tool_calls[:1]}, {"tool_calls": tool_calls[1:]}, {"content": f"MCP_PLUGIN_{phase}_OK"}]
    interactions = [cast(dict[str, list[dict[str, object]]], build_cassette(
        asset_id=f"mcp-plugin-{phase}-{index}", model="mcp-plugin-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-mcp-plugin-{phase}-{index}", model="mcp-plugin-model",
            deltas=({"role": "assistant", **delta},),
            finish_reason="tool_calls" if "tool_calls" in delta else "stop",
        ),
    ).raw)["interactions"][0] for index, delta in enumerate(deltas)]
    ids = case.replay.control.plan_model_calls(session, thread, interactions)
    request_id = f"mcp-plugin-execute-{phase}"
    response = await client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "发现 probe，并发调用 success 和 error，读取进度、结构化与媒体回执后结束。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    }, headers={"X-Request-ID": request_id})
    response.raise_for_status()
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["request_id"] == request_id
    admission = response.json()["data"]
    await wait_for_job_done(client, admission["job_id"], max_attempts=120)
    activity = await client.get(f"/api/v1/sessions/{session}/turns/{admission['turn_id']}/message-stream/events")
    activity.raise_for_status()
    events = activity.json()["data"]
    progress = [event["payload"]["detail"] for event in events
                if event["type"] == "activity.updated" and "progress" in event["payload"].get("detail", {})]
    assert [(detail["progress"], detail["total"]) for detail in progress] == [(1, 2), (2, 2)]
    records = [json.loads((case.replay.artifacts / f"{call_id}.json").read_text(encoding="utf-8")) for call_id in ids]
    messages = records[-1]["request"]["messages"]
    for call in tool_calls[1:]:
        result = next(message for message in messages
                      if message.get("role") == "tool" and message.get("tool_call_id") == call["id"])
        assert "完整协议回执" in json.dumps(result, ensure_ascii=False)
        assert "https://example.invalid/report" in json.dumps(result)
        assert phase in json.dumps(result)
    media = messages[-1]
    assert media["role"] == "user"
    assert [block["type"] for block in media["content"]] == ["text", "image_url", "input_audio"] * 2
    for offset, call in zip((0, 3), tool_calls[1:], strict=True):
        assert call["id"] in media["content"][offset]["text"]
        assert media["content"][offset + 1]["image_url"]["url"] == f"data:image/png;base64,{RESULT_IMAGE_BASE64}"
        assert media["content"][offset + 2]["input_audio"] == {"data": RESULT_AUDIO_BASE64, "format": "wav"}
    with RolloutCheckpointSaver(case.workspace_root / ".boxteam/sessions") as cold:
        items = cold._storage.storage.read_items(session, thread)
        for mode, call in zip(("success", "error"), tool_calls[1:], strict=True):
            matching = [item for item in items if item.semantic_kind == "tool_result"
                        and item.tool_call_id is not None and item.tool_call_id.endswith(call["id"])]
            assert matching
            for item in matching:
                assert isinstance(item.payload, dict)
                assert item.payload["tool_outcome"] == ("success" if mode == "success" else "failure")
                assert item.payload["artifact"] == {"structured_content": {"receipt": {
                    "name": "probe", "arguments": {"result_mode": mode, "phase": phase,
                        **({"call_mode": "progress"} if mode == "success" else {})},
                }}}
                assert item.projection_identity is not None
                assert item.projection_identity.model_call_id == records[1]["identity"]["model_call_id"]
    return records


async def test_stdio_plugin_preserves_results_and_survives_backend_restart(
    mcp_plugin_case: McpPluginCase, http_schema_endpoint_factory, prepared_mcp_stdio_package: Path,
) -> None:
    case = mcp_plugin_case
    evidence: dict[str, object] = {}
    artifacts = case.context.artifacts_dir
    definitions = artifacts / "definitions.json"
    receipts = artifacts / "receipts.jsonl"
    definitions.write_text(json.dumps({"probe": {"type": "object"}, "second": {"type": "object"}}), encoding="utf-8")
    archive = _package(artifacts / "mcp-bridge.zip", prepared_mcp_stdio_package)
    try:
        await install_plugin_async(
            lambda path, payload: case.client.post(path, json=payload),
            gateway_id=case.gateway_id,
            path=str(archive),
            expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        await _publish(case, {"plugins": {
            "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "enabled": True}]},
            "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
                "command": sys.executable, "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"), str(definitions), str(receipts), "--page-size", "1"],
                "cwd": str(Path.cwd()),
            }}}},
        }}, "mcp-plugin-enable")
        plugin = await _plugin(case, state="ready")
        assert plugin["state"] == "ready", plugin
        await _publish(case, {"mcp": {"servers": {"probe": {
            "enabled": True, "transport": "plugin", "service": {
                "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
                "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "service_id": "mcp",
            },
        }}}}, "mcp-plugin-connect")
        before = await _catalog(case)
        await _agent_call(case, phase="before")
        instance = plugin["instance"]
        plugin_process = psutil.Process(instance["process_id"])
        child_processes = plugin_process.children(recursive=True)
        children = {process.pid for process in child_processes}
        assert children, "stdio Server 必须实际由插件进程持有"
        evidence.update(plugin_before=plugin, catalog_before=before, stdio_children_before=sorted(children))
        case.client.headers.pop("X-BoxTeam-Target-Gateway-Id", None)
        case.client.headers.pop("X-BoxTeam-Gateway-Id", None)
        case.client.headers.pop("X-BoxTeam-Workspace-Id", None)
        case.restart_gateway()
        _, old_alive = await asyncio.to_thread(
            psutil.wait_procs, [plugin_process, *child_processes], timeout=5,
        )
        assert not old_alive, [item.pid for item in old_alive]
        response = await case.client.get("/api/gateway/workspaces")
        response.raise_for_status()
        active = response.json()["data"]["active_workspace"]
        assert active["workspace_id"] == case.workspace_id
        case.client.headers.update({
            "X-BoxTeam-Target-Gateway-Id": active["gateway_id"],
            "X-BoxTeam-Gateway-Id": active["gateway_id"],
            "X-BoxTeam-Workspace-Id": active["workspace_id"],
        })
        cold_plugin = await _plugin(case, state="ready")
        assert cold_plugin["instance"]["instance_id"] != instance["instance_id"]
        cold_catalog = await _catalog(case)
        await _agent_call(case, phase="cold-start")
        instance = cold_plugin["instance"]
        plugin_process = psutil.Process(instance["process_id"])
        child_processes = plugin_process.children(recursive=True)
        children = {process.pid for process in child_processes}
        assert children, "冷启动后 stdio Server 必须由新插件进程持有"
        evidence.update(cold_plugin=cold_plugin, cold_catalog=cold_catalog,
                        cold_start_receipts=receipts.read_text(encoding="utf-8").splitlines())
        response = await case.client.get("/api/gateway/workspaces")
        response.raise_for_status()
        workspace = next(
            item for item in response.json()["data"]["items"]
            if item["workspace_id"] == case.workspace_id
        )
        backend_generation_before = workspace["backend_generation"]
        assert backend_generation_before
        response = await case.client.post(
            f"/api/gateway/workspaces/{case.workspace_id}/runtime/restart-safe", timeout=150,
        )
        response.raise_for_status()
        assert response.json()["data"]["backend_generation"] != backend_generation_before
        after_plugin = await _plugin(case, state="ready")
        assert after_plugin["instance"] == cold_plugin["instance"]
        assert {process.pid for process in psutil.Process(instance["process_id"]).children(recursive=True)} == children
        after = await _catalog(case)
        assert after["connection_ref"] != cold_catalog["connection_ref"], (
            "Backend 重启必须建立新的逻辑 client generation"
        )
        await _agent_call(case, phase="after")
        actual = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
        assert sorted(actual, key=lambda row: (row["arguments"]["phase"], row["arguments"]["result_mode"])) == [
            {"name": "probe", "arguments": {"result_mode": mode, "phase": phase, **({"call_mode": "progress"} if mode == "success" else {})}}
            for phase in ("after", "before", "cold-start") for mode in ("error", "success")
        ]
        evidence.update(plugin_after=after_plugin, catalog_after=after, receipts=actual)
        observed: list[dict[str, str]] = []
        http_requests: list[dict[str, str]] = []
        async with http_schema_endpoint_factory(
            "external", required_headers={"X-MCP-Target": "external-probe"},
            request_observer=lambda headers: http_requests.append(dict(headers)),
            call_observer=lambda _name, _arguments, headers: observed.append(dict(headers)),
        ) as external:
            await _publish(case, {"mcp": {"servers": {"probe": {
                "enabled": True, "transport": "streamable_http", "url": external.url,
                "headers": {"X-MCP-Target": "external-probe"},
            }}}}, "mcp-external-connect")
            response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
            response.raise_for_status()
            remote, = response.json()["data"]
            assert remote["server_id"] == "probe" and remote["status"] == "ready"
            assert remote["transport"] == "streamable_http" and remote.get("service") is None
            await _agent_call(case, phase="external")
            assert observed and observed[0]["x-mcp-target"] == "external-probe"
            assert "x-local-token" not in observed[0], "Gateway 管理凭据不能交付第三方 MCP"
            assert http_requests and all(headers.get("x-request-id") for headers in http_requests), (
                "外部 MCP 的所有 HTTP 请求必须经过 Gateway 的原 TraceMiddleware 和代理链"
            )
            external_receipts = [json.loads(line) for line in external.receipts.read_text(encoding="utf-8").splitlines()]
            assert sorted(external_receipts, key=lambda row: row["arguments"]["result_mode"]) == [
                {"name": "probe", "arguments": {"result_mode": mode, "phase": "external", **({"call_mode": "progress"} if mode == "success" else {})}}
                for mode in ("error", "success")
            ]
            assert (await _plugin(case))["instance"] == after_plugin["instance"]
            assert {process.pid for process in psutil.Process(instance["process_id"]).children(recursive=True)} == children
            evidence.update(external_catalog=remote, external_receipts=external_receipts,
                            external_request_ids=[headers["x-request-id"] for headers in http_requests])
            # 先沿产品入口结清连接，再关闭外部 HTTP 资产。
            await _publish(case, {"mcp": {"servers": {}}}, "mcp-disconnect-external")
        await _publish(case, {"plugins": {
            "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "enabled": False}]},
        }, "mcp": {"servers": {"probe": {
            "enabled": False, "transport": "plugin", "service": {
                "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
                "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "service_id": "mcp",
            },
        }}}}, "mcp-disabled-offline-config")
        stopped = await _plugin(case, state="stopped")
        assert stopped["state"] == "stopped" and stopped["instance"] is None
        _, alive = await asyncio.to_thread(psutil.wait_procs, [plugin_process, *child_processes], timeout=5)
        assert not alive, f"关闭插件后实际进程未退出: {[process.pid for process in alive]}"
        response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
        response.raise_for_status()
        disabled, = response.json()["data"]
        assert disabled["status"] != "ready" and disabled["tools"] == []
        old_route = (f"/api/gateway/service-plugins/boxteam.mcp-stdio/stdio/instances/{instance['instance_id']}/services/mcp/"
                     f"?package_version=0.1.0&workspace_id={case.workspace_id}")
        response = await case.client.post(old_route, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 409, response.text
        assert (await _plugin(case))["instance"] is None, "普通读取和失效调用不得重新启动插件"
        evidence.update(plugin_stopped=stopped, disabled_catalog=disabled)
    finally:
        (artifacts / "mcp-plugin-lifecycle.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
