"""管理目录从原 registry 投影零成员连接及真实来源，不暴露凭据。"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes import workspaces


@pytest.fixture
def management_registry(tmp_path: Path) -> Iterator[GatewayWorkspaceRegistry]:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id="gateway_" + "1" * 32)
    for index, owner in enumerate(("manual", "config"), 2):
        registry.upsert_remote_gateway(RemoteGatewayConnection(
            connection_id=f"rgw_{owner}", name=f"{owner} 连接", host="example.test", port=22,
            username="developer", private_key_path="/private/key", ssh_config_host=None,
            remote_gateway_port=9000 + index, remote_gateway_id="gateway_" + str(index) * 32,
            protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner=owner,
            connection_error="stage=restore, error=RuntimeError" if owner == "config" else None,
        ))
    try:
        yield registry
    finally:
        registry.close()


@pytest.fixture
def management_app(management_registry: GatewayWorkspaceRegistry) -> FastAPI:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(workspaces.router)
    application.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    application.dependency_overrides[workspaces.get_registry] = lambda: management_registry
    return application


@pytest.mark.asyncio
async def test_management_directory_serializes_zero_workspace_connections(
    management_app: FastAPI, management_registry: GatewayWorkspaceRegistry,
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app), base_url="http://gateway") as client:
        response = await client.get("/api/gateway/management/gateways")
    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == response.headers["X-Request-ID"]
    directory = payload["data"]
    assert directory["local_gateway_id"] == management_registry.gateway_id
    local, manual, config = directory["items"]
    assert local["source_owner"] is None and local["remote_gateway_port"] is None
    assert manual["source_owner"] == "manual" and manual["remote_gateway_port"] == 9002
    assert config["source_owner"] == "config" and config["remote_gateway_port"] == 9003
    assert manual["gateway_connection_id"] == "rgw_manual"
    assert config["connection_error"] == "stage=restore, error=RuntimeError"
    assert management_registry.targets() == ()
    assert "/private/key" not in response.text and "token" not in response.text
