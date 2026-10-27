"""真实页面由产品入口产生拒绝、取消、原期限到期和断开状态。"""

from __future__ import annotations

import json

from tests.integration.clients.web.test_mcp_connection_authorization import (
    browser_runner as browser_runner,  # noqa: PLC0414 - 复用同一 Chromium 场景和初始化校验
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 原 MCP handler
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一 OAuth/PKCE HTTP 资产
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    product_case as product_case,  # noqa: PLC0414 - 同一真实 Gateway/Backend 装具
)
from tests.integration.clients.web.test_mcp_connection_authorization import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 按本文件隔离的正式工作区
)
from tests.support.api_helpers import create_session


async def test_web_denial_original_deadline_and_disconnect_keep_authoritative_state(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True) as (client, _replay, case),
    ):
        secure.deny_next_authorization = True
        title = "MCP 授权拒绝与真实期限"
        await create_session(client, title)
        ledger_path = case.mount.workspace_root / ".boxteam/resources.json"
        before = {item["resource_id"]: item for item in json.loads(ledger_path.read_text())["records"]}
        observed = await browser_runner(case, base_url=str(client.base_url), session_title=title, mode="errors")
        response = await client.get("/api/v1/mcp/connection-operations")
        assert response.status_code == 200, response.text
        latest, = response.json()["data"]
        assert latest["operation_id"] == observed["operationIds"][-1]
        ordered = []
        assert len(set(observed["operationIds"])) == 4
        for identity in observed["operationIds"]:
            response = await client.get(f"/api/v1/mcp/connection-operations/{identity}")
            assert response.status_code == 200, response.text
            operation = response.json()["data"]
            assert operation["operation_id"] == identity
            ordered.append(operation)
        assert [(item["action"], item["status"], item["error_code"]) for item in ordered] == [
            ("connect", "cancelled", "operation_cancelled"), ("connect", "expired", "deadline_exceeded"),
            ("connect", "succeeded", None), ("disconnect", "succeeded", None),
        ]
        assert observed["denialVisible"] and observed["expiryVisible"] and observed["disconnectVerified"]
        assert observed["noPageErrors"] and observed["siblingReady"]
        assert ordered[1]["deadline"] == observed["deadline"]
        assert observed["expiryObservedAt"] >= ordered[1]["deadline"]
        assert 299 < ordered[1]["deadline"] - ordered[1]["created_at"] < 301
        assert secure.denials == secure.exchanges == len(secure.authorizations) == 1
        assert not secure.receipts.exists() or not secure.receipts.read_text().strip()
        response = await client.get("/api/v1/mcp/servers")
        assert response.status_code == 200, response.text
        servers = {item["server_id"]: item for item in response.json()["data"]}
        assert servers["secure"]["status"] != "ready" and servers["schema"]["status"] == "ready"
        after = {item["resource_id"]: item for item in json.loads(ledger_path.read_text())["records"]}
        closed, = [item for identity, item in after.items() if identity not in before]
        assert closed["kind"] == "mcp_connection" and closed["status"] == "stopped"
        assert all(after[identity]["status"] == item["status"] == "running" for identity, item in before.items())
        (case.context.artifacts_dir / "error-owner.json").write_text(json.dumps({
            "operations": ordered, "expiry_observed_after_deadline_seconds": observed["expiryObservedAt"] - observed["deadline"],
            "denials": secure.denials, "exchanges": secure.exchanges, "closed_resource": closed,
        }, indent=2))
