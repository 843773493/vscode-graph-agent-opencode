"""真实 Backend 产品入口产生授权操作，核对持久重放与重启对账。"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.integration.backend.sessions.test_mcp_config_recovery import (
    RecoveryCase,
    ready,
)
from tests.integration.backend.sessions.test_mcp_config_recovery import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 正式测试文件自己的工作区
)
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 共享唯一 OAuth 协议资产
)
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 共享 MCP handler
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


async def observe(client: httpx.AsyncClient, operation_id: str, statuses: set[str]) -> dict[str, object]:
    async with asyncio.timeout(12):
        while True:
            response = await client.get(f"/mcp/connection-operations/{operation_id}", params={"wait_timeout_ms": 500})
            assert response.status_code == 200, response.text
            result = response.json()["data"]
            if result["status"] in statuses:
                return result
            assert result["status"] not in {"failed", "expired", "cancelled"}, result


@pytest.fixture
def configured_case(recovery_case: RecoveryCase):
    def configure(secure, sibling) -> RecoveryCase:
        (recovery_case.mount.workspace_root / ".boxteam/workspace.jsonc").write_text(json.dumps({"mcp": {"servers": {
            "schema": {"enabled": True, "transport": "streamable_http", "url": sibling.url},
            "secure": secure.configuration(),
        }}}), encoding="utf-8")
        return recovery_case
    return configure


@pytest.fixture
def client_factory():
    def create(base_url: str, case: RecoveryCase):
        return httpx.AsyncClient(
            base_url=f"{base_url}/api/v1/workspaces/{case.mount.workspace_id}",
            headers=LOCAL_TOKEN_HEADERS, timeout=15,
        )
    return create


async def test_oauth_operations_replay_callback_catalog_disconnect_and_restart(
    configured_case, oauth_endpoint_factory, http_schema_endpoint_factory, client_factory,
):
    async with oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling:
        case = configured_case(secure, sibling)
        request = {"operation_id": "connect-intent", "server_id": "secure", "action": "connect"}
        async with case.process("recover") as (process, reports, base_url):
            await ready(process, reports)
            async with client_factory(base_url, case) as client:
                response = await client.post("/mcp/connection-operations", json=request, headers={"X-Request-ID": "connect-proof"})
                assert response.status_code == 200, response.text
                assert response.headers["x-request-id"] == response.json()["request_id"] == "connect-proof"
                operation = await observe(client, "connect-intent", {"waiting_user"})
                assert operation["connection_id"] == f"mcp-server:v1:{case.mount.workspace_id}:secure"
                for identity in ("connect-intent", "joined-intent"):
                    replay = await client.post("/mcp/connection-operations", json={**request, "operation_id": identity})
                    assert replay.json()["data"]["operation_id"] == "connect-intent"
                authorization = await client.get("/mcp/connection-operations/joined-intent/authorization")
                assert authorization.status_code == 200 and authorization.headers["cache-control"] == "no-store"
                url = authorization.json()["data"]["authorization_url"]
                values = parse_qs(urlparse(url).query)
                # 只读短等待到期不取消授权；原 state 不能进入普通响应。
                waiting = await client.get("/mcp/connection-operations/connect-intent", params={"wait_timeout_ms": 20})
                assert waiting.json()["data"]["status"] == "waiting_user" and values["state"][0] not in waiting.text
                invalid = await client.post("/mcp/connection-operations/connect-intent/callback", json={"code": "private-invalid-code", "state": "错误-state"})
                assert invalid.status_code == 400 and "private-invalid-code" not in invalid.text
                malformed = await client.post("/mcp/connection-operations/connect-intent/callback", json={"code": "private-validation-marker", "state": []})
                assert malformed.status_code == 422 and "private-validation-marker" not in malformed.text
                async with httpx.AsyncClient() as browser:
                    redirect = await browser.get(url)
                assert redirect.status_code == 303
                callback = {name: items[0] for name, items in parse_qs(urlparse(redirect.headers["location"]).query).items()}
                result = await client.post("/mcp/connection-operations/joined-intent/callback", json=callback)
                assert result.status_code == 200, result.text
                duplicate = await client.post("/mcp/connection-operations/connect-intent/callback", json=callback)
                assert duplicate.status_code in {400, 409}
                complete = await observe(client, "joined-intent", {"succeeded"})
                assert complete["stage"] == "complete" and secure.exchanges == secure.registrations == 1
                servers = await client.get("/mcp/servers")
                secure_status = next(item for item in servers.json()["data"] if item["server_id"] == "secure")
                assert secure_status["status"] == "ready" and secure_status["tools"][0]["tool_id"] == "mcp__secure__probe"
                replay = await client.post("/mcp/connection-operations", json={**request, "operation_id": "joined-intent"})
                assert replay.json()["data"] == complete and secure.exchanges == 1
                disconnected = await client.post("/mcp/connection-operations", json={**request, "action": "disconnect", "operation_id": "disconnect-intent"})
                assert disconnected.status_code == 200
                await observe(client, "disconnect-intent", {"succeeded"})
                servers = await client.get("/mcp/servers")
                assert next(item for item in servers.json()["data"] if item["server_id"] == "secure")["tools"] == []
                assert next(item for item in servers.json()["data"] if item["server_id"] == "schema")["status"] == "ready"
            raw = (reports / "process.raw").read_text()
            assert all(secret not in raw for secret in (values["state"][0], callback["code"], secure.access_token, secure.refresh_token, "private-validation-marker"))
        # 运行时 disconnect 不改 JSONC；进程重启按已启用配置重建，操作终态不改写。
        async with case.process("recover") as (process, reports, base_url):
            await ready(process, reports)
            async with client_factory(base_url, case) as client:
                replay = await client.post("/mcp/connection-operations", json={**request, "operation_id": "joined-intent"})
                assert replay.json()["data"] == complete and secure.exchanges == 1
                servers = await client.get("/mcp/servers")
                assert next(item for item in servers.json()["data"] if item["server_id"] == "secure")["status"] == "ready"


async def test_cancel_and_abrupt_restart_do_not_replay_authorization(
    configured_case, oauth_endpoint_factory, http_schema_endpoint_factory, client_factory,
):
    async with oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling:
        case = configured_case(secure, sibling)
        request = {"operation_id": "cancel-intent", "server_id": "secure", "action": "connect"}
        async with case.process("recover") as (process, reports, base_url):
            await ready(process, reports)
            async with client_factory(base_url, case) as client:
                assert (await client.post("/mcp/connection-operations", json=request)).status_code == 200
                await observe(client, "cancel-intent", {"waiting_user"})
                cancelled = await client.post("/mcp/connection-operations/cancel-intent/cancel")
                assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
                assert (await client.get("/mcp/connection-operations/cancel-intent/authorization")).status_code == 409
                replay = await client.post("/mcp/connection-operations", json=request)
                assert replay.json()["data"] == cancelled.json()["data"]
                assert (await client.post("/mcp/connection-operations", json={**request, "operation_id": "lost-intent"})).status_code == 200
                await observe(client, "lost-intent", {"waiting_user"})
            # 关键等待由真实产品入口产生；OS 级退出不执行优雅 shutdown。
            process.kill()
            await asyncio.to_thread(process.wait, 5)
        async with case.process("recover") as (process, reports, base_url):
            await ready(process, reports)
            async with client_factory(base_url, case) as client:
                operation = (await client.get("/mcp/connection-operations/lost-intent")).json()["data"]
                assert operation["status"] == "failed" and operation["stage"] == "reconcile"
                assert operation["error_code"] == "process_restarted_unconfirmed"
                replay = await client.post("/mcp/connection-operations", json={**request, "operation_id": "lost-intent"})
                assert replay.json()["data"] == operation and secure.exchanges == 0
                assert (await client.get("/mcp/connection-operations/lost-intent/authorization")).status_code == 409


@pytest.mark.parametrize("failure", ["catalog", "token"])
async def test_oauth_exchange_is_not_connection_readiness(
    configured_case, oauth_endpoint_factory, http_schema_endpoint_factory, client_factory, failure: str,
):
    async with oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling:
        case = configured_case(secure, sibling)
        async with case.process("recover") as (process, reports, base_url):
            await ready(process, reports)
            if failure == "catalog":
                secure.definitions.write_text(json.dumps({"probe": {"type": "invalid-type"}}))
            else:
                secure.fail_token = True
            async with client_factory(base_url, case) as client:
                response = await client.post("/mcp/connection-operations", json={"operation_id": "failure-intent", "server_id": "secure", "action": "connect"})
                assert response.status_code == 200
                await observe(client, "failure-intent", {"waiting_user"})
                url = (await client.get("/mcp/connection-operations/failure-intent/authorization")).json()["data"]["authorization_url"]
                async with httpx.AsyncClient() as browser:
                    redirect = await browser.get(url)
                callback = {name: items[0] for name, items in parse_qs(urlparse(redirect.headers["location"]).query).items()}
                assert (await client.post("/mcp/connection-operations/failure-intent/callback", json=callback)).status_code == 200
                operation = await observe(client, "failure-intent", {"failed"})
                assert operation["stage"] == ("catalog" if failure == "catalog" else "initialize")
                assert secure.exchanges == (1 if failure == "catalog" else 0)
                servers = (await client.get("/mcp/servers")).json()["data"]
                assert next(item for item in servers if item["server_id"] == "secure")["status"] != "ready"
                assert next(item for item in servers if item["server_id"] == "schema")["status"] == "ready"
            assert all(secret not in (reports / "process.raw").read_text() for secret in (secure.access_token, secure.refresh_token, secure.private_marker))
