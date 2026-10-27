"""复用原 Web 授权场景，通过真实 SSH 联邦把回调交给远端工作区。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    browser_runner as browser_runner,  # noqa: PLC0414 - 同一浏览器交互及秘密边界
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    discovery_plan as discovery_plan,  # noqa: PLC0414 - 同一 model_stream 响应资产
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 原 MCP handler
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 原 OAuth/PKCE 服务
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    test_web_authorization_reaches_original_owner_then_discovers_and_executes as _verify_web_authorization,
)
from tests.support.communication.federation import open_communication_triad
from tests.support.model_stream.config import model_stream_replay
from tests.support.ports import integration_port_block_for_file


@pytest.fixture
def product_case(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(context.test_file)
    boundary = request.param

    @asynccontextmanager
    async def start(secure, sibling, *, web):
        assert web is True

        def prepare(node_context, model_port, gateway_port):
            workspace = _prepare_workspace(node_context, model_port, gateway_port)
            path = workspace / ".boxteam/workspace.jsonc"
            payload = read_jsonc_object(path)
            payload["llm"] = {"providers": [{
                "id": "control-replay", "endpoint": "https://opencode.ai/zen/v1", "model": "control-replay-model",
                "api_key": "control-replay-key", "custom_llm_provider": "openai",
                "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True},
                             "supports_reasoning": {"reasoning_content": False}},
            }]}
            agents = payload["agents"]
            assert isinstance(agents, dict)
            for agent in agents.values():
                assert isinstance(agent, dict)
                agent["model"] = {"primary_provider": "control-replay", "fallback_providers": []}
            tools = agents["default"].setdefault("tools", {})
            tools["custom"] = [{"tool_id": "connections.manage"}]
            payload["mcp"] = {"servers": {"schema": {
                "enabled": True, "transport": "streamable_http", "url": sibling.url,
            }}}
            if node_context.workspace_root.parent.name == "b":
                payload["mcp"]["servers"]["secure"] = secure.configuration(
                    redirect_uri=f"http://127.0.0.1:{ports.port(1)}/mcp-oauth-callback.html",
                )
            payload["context"] = {"resource_activation": {
                "default_boundary": boundary, "overrides": {"mcp_tool_catalog": boundary},
            }}
            path.write_text(json.dumps(payload), encoding="utf-8")
            return workspace

        with model_stream_replay(
            context.artifacts_dir / "model-replay", runtime_root=context.runtime_root_for_node(request.node.nodeid),
            business_assertion="mcp-remote-gateway-web-authorization",
        ) as replay:
            async with open_communication_triad(
                context, prepare, ports=ports, backend_asgi_app="tests.support.e2e_control.backend_app:app",
                backend_environment={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
                gateway_environment={"BOXTEAM_WEB_ASSETS": str(context.project_root / "src/clients/web/dist")},
            ) as nodes:
                hub, target = nodes["a"], nodes["b"]
                listing = await hub.client.get("/api/gateway/workspaces")
                assert listing.status_code == 200, listing.text
                routes = {item["backend_workspace_id"]: item for item in listing.json()["data"]["items"]}
                remote = routes[target.workspace_id]
                assert remote["remote"]["gateway_id"] == target.gateway_id
                assert remote["workspace_id"] != target.workspace_id
                activated = await hub.client.post(f"/api/gateway/workspaces/{remote['workspace_id']}/activate")
                assert activated.status_code == 200, activated.text
                hub.client.headers["X-BoxTeam-Workspace-Id"] = remote["workspace_id"]
                case = SimpleNamespace(context=context, mount=MountedWorkspace(target.workspace_id, target.workspace))
                yield hub.client, replay, case

                operations = await target.client.get("/api/v1/mcp/connection-operations")
                assert operations.status_code == 200, operations.text
                operation, = operations.json()["data"]
                assert operation["status"] == "succeeded"
                for name in ("a", "c"):
                    local_headers = {"X-BoxTeam-Workspace-Id": routes[hub.workspace_id]["workspace_id"],
                                     "X-BoxTeam-Backend-Workspace-Id": hub.workspace_id} if name == "a" else {}
                    absent = await nodes[name].client.get(
                        f"/api/v1/mcp/connection-operations/{operation['operation_id']}", headers=local_headers,
                    )
                    assert absent.status_code == 404, absent.text
                    assert not list((nodes[name].workspace / ".boxteam/credentials/mcp-oauth").glob("*.json"))
                assert len(list((target.workspace / ".boxteam/credentials/mcp-oauth").glob("*.json"))) == 1
                secrets = [secure.authorizations[0]["code"], secure.authorizations[0]["state"], secure.access_token, secure.refresh_token]
                for name, node in nodes.items():
                    for log_root in (node.workspace / ".boxteam/logs", context.artifacts_dir / f"gateway-{name}"):
                        for path in log_root.rglob("*.log"):
                            content = path.read_text(errors="replace")
                            assert all(secret not in content for secret in secrets), path.name
                (context.artifacts_dir / "remote-owner.json").write_text(json.dumps({
                    "boundary": boundary, "hub_gateway_id": hub.gateway_id, "owner_gateway_id": target.gateway_id,
                    "route_workspace_id": remote["workspace_id"], "backend_workspace_id": target.workspace_id,
                    "operation_id": operation["operation_id"], "credential_owner_only": True,
                }, indent=2))
    return start


@pytest.mark.parametrize("product_case", ["turn", "model_call"], indirect=True)
async def test_remote_gateway_callback_reaches_original_workspace(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan, browser_runner,
):
    await _verify_web_authorization(
        product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan, browser_runner,
    )
