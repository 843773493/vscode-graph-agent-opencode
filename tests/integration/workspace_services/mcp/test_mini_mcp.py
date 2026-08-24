from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from tests.support.api_waiters import wait_for_job_done
from tests.support.mcp_configuration import write_mcp_config
from tests.support.messages import last_assistant_message


@pytest.fixture(scope="module")
def integration_config_path(
    integration_workspace_root_path: str,
) -> str:
    project_root = Path.cwd().resolve()
    mini_server_path = (
        project_root
        / "tests"
        / "integration"
        / "workspace_services"
        / "mcp"
        / "mini_server.py"
    )
    return write_mcp_config(
        workspace_root=Path(integration_workspace_root_path),
        servers={
            "mini": {
                "enabled": True,
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(mini_server_path)],
                "cwd": str(project_root),
            }
        },
        allowed_mcp_tools=["mcp__mini__increment"],
    )


@pytest.mark.asyncio
async def test_agent_uses_test_started_stateful_mini_mcp(
    integration_client: httpx.AsyncClient,
) -> None:
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

    observed_results: list[str] = []
    for invocation_index in (1, 2):
        message_response = await integration_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": (
                        "必须调用工具 mcp__mini__increment，"
                        f"这是第 {invocation_index} 次调用；请通过 invoke_custom_tool，"
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
        observed_results.append(
            last_assistant_message(messages_response.json()["data"]["items"])
        )

    assert "1" in observed_results[0]
    assert "2" in observed_results[1]

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
