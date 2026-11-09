from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from mcp import types

from tests.support.api_waiters import wait_for_job_done
from tests.support.mcp.configuration import write_mcp_config
from tests.support.mcp.http_endpoint import HttpSchemaEndpoint
from tests.support.messages import last_assistant_message
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)


@pytest.fixture(autouse=True)
def use_test_gateway_home(
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boxteam_home = (
        Path(integration_workspace_root_path).resolve().parent / "boxteam-home"
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))


@pytest.fixture
async def mini_mcp_case(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    integration_backend_port: int,
    http_schema_endpoint_factory,
    mcp_gateway_config,
) -> AsyncIterator[tuple[httpx.AsyncClient, HttpSchemaEndpoint]]:
    workspace_root = Path(integration_workspace_root_path)
    count = 0

    def provide_result(
        name: str, arguments: dict[str, object]
    ) -> list[types.TextContent]:
        nonlocal count
        assert name == "increment" and arguments == {}
        count += 1
        return [types.TextContent(type="text", text=str(count))]

    async with http_schema_endpoint_factory(
        "mini",
        tool_definitions={
            "increment": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        call_result_provider=provide_result,
    ) as endpoint:
        generated_config = Path(
            write_mcp_config(
                workspace_root=workspace_root,
                servers={
                    "mini": {
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": endpoint.url,
                    }
                },
                allowed_mcp_tools=["mcp__mini__increment"],
            )
        )
        workspace_config = Path(integration_workspace_config_path)
        workspace_config.write_text(
            generated_config.read_text(encoding="utf-8"), encoding="utf-8"
        )
        backend = await asyncio.to_thread(
            start_backend_process,
            workspace_root=integration_workspace_root_path,
            port=integration_backend_port,
            log_name="integration-backend",
            boxteam_home=workspace_root.resolve().parent / "boxteam-home",
            env_overrides={
                "BOXTEAM_GATEWAY_URL": mcp_gateway_config.get_gateway_connection_url(),
            },
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{integration_backend_port}",
                timeout=60,
                headers={
                    "X-Local-Token": "local-dev-token",
                    "X-BoxTeam-Workspace-Id": workspace_backend_id_for_root(
                        integration_workspace_root_path
                    ),
                },
            ) as client:
                yield client, endpoint
        finally:
            await asyncio.to_thread(close_backend_process, backend)


@pytest.mark.asyncio
async def test_agent_reuses_mini_mcp_state_across_tool_calls(
    mini_mcp_case: tuple[httpx.AsyncClient, HttpSchemaEndpoint],
) -> None:
    integration_client, endpoint = mini_mcp_case
    servers_response = await integration_client.get("/api/v1/mcp/servers")
    assert servers_response.status_code == 200
    servers = servers_response.json()["data"]
    mini_server = next(item for item in servers if item["server_id"] == "mini")
    assert mini_server["status"] == "ready"
    increment_tool = next(
        item for item in mini_server["tools"] if item["remote_name"] == "increment"
    )
    assert increment_tool["tool_id"] == "mcp__mini__increment"

    create_response = await integration_client.post(
        "/api/v1/sessions",
        json={"title": "Mini MCP 集成测试"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    assistant_responses: list[str] = []
    for invocation_index in (1, 2):
        message_response = await integration_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": (
                        "必须调用工具 mcp__mini__increment，"
                        f"这是第 {invocation_index} 次调用；请通过 invoke_extension_tool，"
                        "传入 tool_name=mcp__mini__increment 和 arguments={}；不得猜测结果。"
                    )
                },
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        )
        assert message_response.status_code == 200
        await wait_for_job_done(
            integration_client,
            message_response.json()["data"]["job_id"],
            max_attempts=180,
        )
        messages_response = await integration_client.get(
            f"/api/v1/sessions/{session_id}/messages"
        )
        assert messages_response.status_code == 200
        assistant_responses.append(
            last_assistant_message(messages_response.json()["data"]["items"])
        )

    assert "1" in assistant_responses[0]
    assert "2" in assistant_responses[1]

    tools_response = await integration_client.get("/api/v1/tools")
    assert tools_response.status_code == 200
    catalog_tool = next(
        item
        for item in tools_response.json()["data"]
        if item["tool_id"] == increment_tool["tool_id"]
    )
    assert catalog_tool["group_id"] == "mcp:mini"
    assert catalog_tool["group_name"] == "扩展工具 · MCP · mini"
    assert catalog_tool["kind"] == "extension"
    assert [
        json.loads(line)
        for line in endpoint.receipts.read_text(encoding="utf-8").splitlines()
    ] == [
        {"name": "increment", "arguments": {}},
        {"name": "increment", "arguments": {}},
    ]
