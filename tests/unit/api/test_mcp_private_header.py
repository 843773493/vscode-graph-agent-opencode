"""工作区私有 header 只保存不可见值并返回绑定引用。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api import mcp
from app.api.deps import (
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    verify_local_token,
)
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

GATEWAY = "gateway_" + "a" * 32
SECRET = "Bearer private-api-unit-only"


@pytest.fixture
def private_api(empty_workspace_scope: tuple[str, Path]) -> Iterator[tuple[TestClient, dict[str, object], Path]]:
    from app.main import app as workspace_app

    workspace_id, root = empty_workspace_scope
    owner = McpCatalogOwner(raw_config={}, workspace_root=root,
                            event_service=EventChannelService(), leases=ExternalResourceLeaseLedger())
    application = FastAPI()
    application.state.backend_id = "00000000-0000-4000-8000-000000000011"
    application.add_middleware(TraceMiddleware)
    application.include_router(mcp.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    with WorkspaceStateStore(workspace_root=root) as store:
        service = McpConnectionService(owner=owner, store=store, workspace_id=workspace_id)
        application.dependency_overrides[get_mcp_catalog_owner] = lambda: owner
        application.dependency_overrides[get_mcp_connection_service] = lambda: service
        application.dependency_overrides[verify_local_token] = lambda: "local"
        payload = {"target": {"kind": "workspace", "gateway_id": GATEWAY, "workspace_id": workspace_id},
                   "server_id": "secure", "header_name": "Authorization", "endpoint": "https://service.test/mcp", "value": SECRET}
        with TestClient(application, headers={"X-BoxTeam-Gateway-Id": GATEWAY, "X-Request-ID": "private-api"}) as client:
            yield client, payload, root
    assert not owner.started


def test_private_write_returns_only_bound_ref_and_preserves_existing_configuration(private_api):
    client, payload, root = private_api
    first = client.post("/api/v1/mcp/private-headers", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert first.headers["Cache-Control"] == "no-store"
    assert body["target"] == payload["target"] and body["protocol_version"] == 1
    assert body["request_id"] == first.headers["X-Request-ID"] == "private-api"
    reference = body["data"]
    assert reference["workspace_id"] == payload["target"]["workspace_id"]
    assert {key: reference[key] for key in ("server_id", "header_name", "endpoint")} == {
        key: payload[key] for key in ("server_id", "header_name", "endpoint")
    }
    assert SECRET not in first.text and "value" not in reference
    assert not (root / ".boxteam/workspace.jsonc").exists()
    second = client.post("/api/v1/mcp/private-headers", json={**payload, "value": "Bearer rotated"})
    assert second.status_code == 200 and second.json()["data"]["version"] != reference["version"]


@pytest.mark.parametrize("change", [
    {"value": SECRET + "\r\ninvalid"}, {"value": ""}, {"value": {"nested": SECRET}},
    {"value": " " + SECRET}, {"value": SECRET + " "}, {"value": " " + SECRET + " "},
    {"header_name": "bad\r\nname"}, {"endpoint": "http://remote.test/mcp"},
    {"endpoint": "https://user:private-url-password@service.test/mcp"},
    {"endpoint": "https://user@service.test/mcp"},
    {"endpoint": "https://service.test/mcp#private-fragment"},
])
def test_private_write_rejects_invalid_input_without_response_or_log_disclosure(private_api, change, caplog):
    client, payload, root = private_api
    response = client.post("/api/v1/mcp/private-headers", json={**payload, **change})
    assert response.status_code == 422
    assert SECRET not in response.text and SECRET not in caplog.text
    assert "private-url-password" not in response.text and "private-url-password" not in caplog.text
    assert not (root / ".boxteam/credentials/mcp-headers").exists()


def test_private_write_never_retargets_another_workspace_or_gateway(private_api):
    client, payload, root = private_api
    for target in (
        {**payload["target"], "workspace_id": "00000000-0000-4000-8000-000000000099"},
        {**payload["target"], "gateway_id": "gateway_" + "b" * 32},
    ):
        response = client.post("/api/v1/mcp/private-headers", json={**payload, "target": target})
        assert response.status_code == 409 and SECRET not in response.text
    assert not (root / ".boxteam/credentials/mcp-headers").exists()
