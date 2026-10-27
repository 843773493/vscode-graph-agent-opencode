"""真实 Gateway／Session／Agent 通过固定信封发起首次 OAuth；模型复用 model_stream。"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.testing.model_stream import build_cassette, chat_completion_frames
from configs.installer import install_user_configuration
from configs.runtime import read_jsonc_object
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.sessions.test_mcp_config_recovery import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 使用本文件正式工作区和运行产物
)
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 唯一 OAuth/MCP HTTP 资产
)
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 复用 MCP handler
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream.config import model_stream_replay
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def product_case(recovery_case, free_tcp_port_factory):
    case = recovery_case
    backend_port, gateway_port = free_tcp_port_factory(), free_tcp_port_factory()

    @asynccontextmanager
    async def start(secure, sibling, *, boundary="turn", startup_mount=None, backend_processes=None, tool_restrictions=None):
        home = case.context.boxteam_home_for_node(f"mcp-product-gateway:{case.mount.workspace_id}")
        install_user_configuration(config_root=home / "config", profile="default", project_root=case.context.project_root, force=True)
        path = case.mount.workspace_root / ".boxteam/workspace.jsonc"
        payload = read_jsonc_object(path) if path.exists() else {}
        payload["mcp"] = {"servers": {
            "schema": {"enabled": True, "transport": "streamable_http", "url": sibling.url},
            "secure": secure.configuration(),
        }}
        payload["context"] = {"resource_activation": {"default_boundary": boundary, "overrides": {"mcp_tool_catalog": boundary}}}
        payload["llm"] = {"providers": [{
            "id": "control-replay", "endpoint": "https://opencode.ai/zen/v1", "model": "control-replay-model",
            "api_key": "control-replay-key", "custom_llm_provider": "openai",
            "api_mode": {
                "protocol": "chat_completions", "model_info": {"supports_function_calling": True},
                "supports_reasoning": {"reasoning_content": False},
            },
        }]}
        default = payload.setdefault("agents", {}).setdefault("default", {})
        default["model"] = {"primary_provider": "control-replay", "fallback_providers": []}
        default.setdefault("tools", {})["custom"] = [{"tool_id": "connections.manage"}]
        if tool_restrictions is not None:
            default["tools"].setdefault("policy", {})["restrictions"] = tool_restrictions
        path.write_text(json.dumps(payload), encoding="utf-8")
        with model_stream_replay(
            case.context.artifacts_dir / "model-replay", runtime_root=case.context.artifacts_dir / "replay-runtime",
            business_assertion="mcp-first-authorization-local-control",
        ) as replay:
            backend = await asyncio.to_thread(
                start_backend_process, workspace_root=str(case.mount.workspace_root), port=backend_port,
                log_name="mcp-local-control", asgi_app="tests.support.e2e_control.backend_app:app",
                startup_mount=startup_mount,
                env_overrides={**replay.environment, "BOXTEAM_HOME": str(home), "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}"},
            )
            owned_backends = backend_processes if backend_processes is not None else []
            owned_backends.append(backend)
            try:
                gateway = await asyncio.to_thread(
                    start_gateway_process, workspace_root=case.mount.workspace_root,
                    default_backend_url=f"http://127.0.0.1:{backend_port}", port=gateway_port,
                    boxteam_home=home,
                    artifacts_dir=case.context.artifacts_dir / "gateway",
                )
                try:
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{gateway_port}", timeout=30,
                        headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": case.mount.workspace_id},
                    ) as client:
                        await acquire_gateway_guest(client)
                        yield client, replay, case
                finally:
                    await asyncio.to_thread(close_gateway_process, gateway)
            finally:
                for owned in reversed(owned_backends):
                    await asyncio.to_thread(close_backend_process, owned)
                shutil.copytree(case.mount.workspace_root / ".boxteam/logs", case.context.artifacts_dir / "backend-logs")
    return start


async def test_first_oauth_remains_discoverable_and_connect_uses_real_call_identity(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling) as (client, replay, case),
    ):
        servers = (await client.get("/api/v1/mcp/servers")).json()["data"]
        assert {item["server_id"]: item["status"] for item in servers} == {"schema": "ready", "secure": "authorization_required"}
        session = await create_session(client, "首次 MCP 授权本地控制")
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        connect_call = str(UUID(hex=replay.control.next_uuid(f"model_call/{session}/{thread}", 2)))
        expected = software_tool_operation_id("mcp_connect", session, thread, connect_call, "debug-e2e-call-2")
        operations = [
            ("search_tools", {"tool_ids": ["connections.manage", "mcp__secure__probe", "mcp__schema__probe"]}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "list"}}}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "connect", "server_id": "secure"}}}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "status", "operation_id": expected, "wait_timeout_ms": 100}}}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "connect", "server_id": "secure", "code": "model-cannot-submit-credentials"}}}),
            ("invoke_extension_tool", {"tool_name": "mcp__schema__probe", "arguments": {"proof": "sibling-still-ready"}}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "disconnect", "server_id": "secure"}}}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "status", "operation_id": expected}}}),
        ]
        interactions = []
        for index in range(len(operations) + 1):
            delta = {"content": "等待用户授权，未报告远端就绪。"} if index == len(operations) else {
                "tool_calls": [_tool_call(call_index=index, name=operations[index][0], arguments=operations[index][1])],
            }
            interactions.append(build_cassette(
                asset_id=f"connection-control-{index}", model="control-replay-model",
                url="https://opencode.ai/zen/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-connection-control-{index}", model="control-replay-model",
                    deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if index < len(operations) else "stop",
                ),
            ).raw["interactions"][0])
        call_ids = replay.control.plan_model_calls(session, thread, interactions)
        accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "发现连接控制，列出工作区连接，为 secure 发起授权。不要声称已连接。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        await wait_for_job_done(client, accepted.json()["data"]["job_id"], max_attempts=140)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in call_ids]
        results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == [f"debug-e2e-call-{index}" for index in range(len(operations))]
        found, listed, connected, observed = [json.loads(message["content"]) for message in results[:4]]
        discovered = {item["tool_id"]: item for item in found["results"]}
        assert discovered["connections.manage"]["schema_state"] == "inline_complete"
        assert discovered["connections.manage"]["origin"] == "custom"
        assert discovered["mcp__schema__probe"]["status"] == "available"
        assert discovered["mcp__secure__probe"]["error"]["code"] == "tool-not-found"
        assert {item["server_id"]: item["status"] for item in listed["connections"]} == {"schema": "ready", "secure": "authorization_required"}
        operation = connected["operation"]
        assert call_ids[2] == connect_call
        assert operation["operation_id"] == expected
        assert operation["connection_id"] == f"mcp-server:v1:{case.mount.workspace_id}:secure"
        assert operation["status"] in {"pending", "verifying", "waiting_user"}
        assert observed["operation"]["operation_id"] == expected
        assert observed["operation"]["status"] == "waiting_user"
        assert "参数校验失败" in results[4]["content"]
        sibling_result = results[5]["content"]
        assert len(sibling_result) == 1 and sibling_result[0]["type"] == "text"
        assert json.loads(sibling_result[0]["text"]) == {
            "name": "probe", "arguments": {"proof": "sibling-still-ready"},
        }
        assert json.loads(results[7]["content"])["operation"]["status"] == "cancelled"
        disconnected = json.loads(results[6]["content"])["operation"]
        disconnected_id = software_tool_operation_id("mcp_disconnect", session, thread, call_ids[6], "debug-e2e-call-6")
        assert disconnected["operation_id"] == disconnected_id
        final_disconnect = await client.get(f"/api/v1/mcp/connection-operations/{disconnected_id}")
        assert final_disconnect.status_code == 200, final_disconnect.text
        assert final_disconnect.json()["data"]["status"] == "succeeded"
        status = await client.get(connected["management_path"], headers={"X-Request-ID": "connection-control-proof"})
        assert status.status_code == 200, status.text
        assert status.headers["x-request-id"] == status.json()["request_id"] == "connection-control-proof"
        assert status.json()["data"]["operation_id"] == expected
        assert status.json()["data"]["status"] == "cancelled" and secure.exchanges == 0
        wire = json.dumps(records)
        assert all(secret not in wire for secret in (secure.access_token, secure.refresh_token, "authorization_url", "code_challenge", "code_verifier"))
        assert all(tool["function"]["name"] != "connections.manage" for record in records for tool in record.get("tools", []))
