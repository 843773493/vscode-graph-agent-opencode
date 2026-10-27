"""真实 Backend 启动与状态 API：一个 OAuth Server 不阻断兄弟 Server。"""

from __future__ import annotations

import json

import httpx

from tests.integration.backend.sessions.test_mcp_config_recovery import (
    RecoveryCase,
    ready,
)
from tests.integration.backend.sessions.test_mcp_config_recovery import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 使用当前文件隔离工作区与真实 Backend
)
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 复用唯一 HTTP OAuth 资产
)
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 复用原 MCP handler
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


async def test_first_oauth_startup_keeps_sibling_and_status_api_available(
    recovery_case: RecoveryCase, oauth_endpoint_factory, http_schema_endpoint_factory,
):
    case = recovery_case
    async with oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling:
        path = case.mount.workspace_root / ".boxteam/workspace.jsonc"
        path.write_text(json.dumps({"mcp": {"servers": {
            "schema": {"enabled": True, "transport": "streamable_http", "url": sibling.url},
            "secure": secure.configuration(),
        }}}), encoding="utf-8")
        async with case.process("recover") as (process, reports, base_url):
            startup = await ready(process, reports)
            assert startup["catalog_status"] == "available"
            prefix = f"/api/v1/workspaces/{case.mount.workspace_id}"
            async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=10) as client:
                response = await client.get(prefix + "/mcp/servers", headers={"X-Request-ID": "oauth-startup-proof"})
            assert response.status_code == 200, response.text
            assert response.json()["request_id"] == response.headers["x-request-id"] == "oauth-startup-proof"
            servers = {server["server_id"]: server for server in response.json()["data"]}
            assert servers["secure"]["status"] == "authorization_required" and servers["secure"]["tools"] == []
            assert servers["schema"]["status"] == "ready"
            assert {tool["tool_id"] for tool in servers["schema"]["tools"]} == {"mcp__schema__probe"}
            assert secure.registrations == 1 and secure.exchanges == 0
            assert secure.access_token not in response.text and secure.refresh_token not in response.text
            (case.context.artifacts_dir / "startup-proof.json").write_text(response.text, encoding="utf-8")
