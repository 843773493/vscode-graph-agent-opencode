"""经真实 Gateway／Backend 读取 MCP 目标归属及目录、连接的独立代际。"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - 复用唯一 Gateway／Backend 产品装具
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件独立的正式工作区与产物
)
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 唯一标准 OAuth HTTP 资产
)
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 唯一 schema MCP HTTP 资产
)


async def test_gateway_mcp_receipts_keep_exact_owner_and_independent_generations(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory,
) -> None:
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("schema") as schema,
        product_case(secure, schema) as (client, _replay, case),
    ):
        health = await client.get("/api/gateway/health")
        assert health.status_code == 200, health.text
        gateway_id = health.json()["data"]["gateway_id"]
        expected = {"kind": "workspace", "gateway_id": gateway_id, "workspace_id": case.mount.workspace_id}
        client.headers.update({"X-BoxTeam-Target-Gateway-Id": gateway_id, "X-BoxTeam-Workspace-Id": case.mount.workspace_id})
        registered = await client.get("/api/gateway/workspaces")
        assert registered.status_code == 200, registered.text
        assert any(item["gateway_id"] == gateway_id and item["workspace_id"] == case.mount.workspace_id
                   for item in registered.json()["data"]["items"])
        workspace = await client.get("/api/v1/workspace")
        assert workspace.status_code == 200, workspace.text
        assert workspace.json()["data"]["workspace_id"] == case.mount.workspace_id
        receipts: list[dict[str, object]] = []

        async def request(method, path, **kwargs):
            request_id = f"mcp-target-proof-{len(receipts)}"
            response = await client.request(method, "/api/v1/mcp/" + path, headers={"X-Request-ID": request_id}, **kwargs)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["target"] == expected and body["protocol_version"] == 1
            assert body["request_id"] == response.headers["X-Request-ID"] == request_id
            # 授权 URL 只供用户读取，不写入普通状态证据。
            receipts.append({"method": method, "path": path, "target": body["target"], "request_id": request_id})
            return response, body

        async def observe(operation_id, status):
            async with asyncio.timeout(15):
                while True:
                    _, body = await request("GET", f"connection-operations/{operation_id}", params={"wait_timeout_ms": 100})
                    operation = body["data"]
                    if operation["status"] == status:
                        return operation
                    assert operation["status"] not in {"succeeded", "failed", "expired", "cancelled"}, operation

        _, first = await request("GET", "servers")
        servers = {item["server_id"]: item for item in first["data"]}
        assert servers["schema"]["status"] == "ready" and servers["schema"]["connection_ref"]
        assert servers["secure"]["status"] == "authorization_required" and servers["secure"]["connection_ref"] is None
        _, unchanged = await request("GET", "servers")
        assert (unchanged["catalog_revision"], unchanged["catalog_generation"], unchanged["data"]) == (
            first["catalog_revision"], first["catalog_generation"], first["data"],
        )

        operations = []
        for action in ("disconnect", "connect"):
            identity = "schema-" + action
            intent = {"operation_id": identity, "server_id": "schema", "action": action}
            await request("POST", "connection-operations", json=intent)
            operation = await observe(identity, "succeeded")
            assert operation["operation_id"] == identity
            assert operation["connection_id"] == f"mcp-server:v1:{case.mount.workspace_id}:schema"
            operations.append(operation)
            # 同一 operation 的重复意图仍指向原持久身份和期限。
            _, duplicate = await request("POST", "connection-operations", json=intent)
            assert duplicate["data"] == operation
            _, current = await request("GET", "servers")
            schema_server = next(item for item in current["data"] if item["server_id"] == "schema")
            assert (current["catalog_revision"], current["catalog_generation"]) == (
                first["catalog_revision"], first["catalog_generation"],
            )
            if action == "disconnect":
                assert schema_server["connection_ref"] is None and schema_server["tools"] == []
            else:
                assert schema_server["connection_ref"] and schema_server["connection_ref"] != servers["schema"]["connection_ref"]
        _, listed = await request("GET", "connection-operations")
        assert listed["data"] == [operations[-1]]
        for original in operations:
            _, historical = await request("GET", "connection-operations/" + original["operation_id"])
            assert historical["data"] == original

        schema.definitions.write_text(json.dumps({"probe": {"type": "object"}, "added": {"type": "object"}}))
        _, changed = await request("GET", "servers")
        assert changed["catalog_revision"] != first["catalog_revision"]
        assert changed["catalog_generation"] == first["catalog_generation"] + 1
        changed_schema = next(item for item in changed["data"] if item["server_id"] == "schema")
        assert changed_schema["connection_ref"] == schema_server["connection_ref"]

        await request("POST", "connection-operations", json={"operation_id": "secure-cancel", "server_id": "secure", "action": "connect"})
        await observe("secure-cancel", "waiting_user")
        _, cancelled = await request("POST", "connection-operations/secure-cancel/cancel")
        assert cancelled["data"]["status"] == "cancelled"
        await request("POST", "connection-operations", json={"operation_id": "secure-connect", "server_id": "secure", "action": "connect"})
        await observe("secure-connect", "waiting_user")
        response, authorization = await request("GET", "connection-operations/secure-connect/authorization")
        assert response.headers["Cache-Control"] == "no-store"
        async with httpx.AsyncClient() as browser:
            redirect = await browser.get(authorization["data"]["authorization_url"])
        assert redirect.status_code == 303
        callback = {name: values[0] for name, values in parse_qs(urlparse(redirect.headers["location"]).query).items()}
        await request("POST", "connection-operations/secure-connect/callback", json=callback)
        connected = await observe("secure-connect", "succeeded")
        assert connected["connection_id"] == f"mcp-server:v1:{case.mount.workspace_id}:secure"
        _, final = await request("GET", "servers")
        assert all(item["status"] == "ready" and item["connection_ref"] for item in final["data"])
        assert secure.exchanges == 1
        duplicate_callback = await client.post("/api/v1/mcp/connection-operations/secure-connect/callback", json=callback)
        assert duplicate_callback.status_code in {400, 409}
        assert callback["code"] not in duplicate_callback.text and callback["state"] not in duplicate_callback.text
        (case.context.artifacts_dir / "management-contract.json").write_text(json.dumps({
            "target": expected, "initial": first, "changed": changed, "final": final, "receipts": receipts,
        }, ensure_ascii=False, indent=2) + "\n")
