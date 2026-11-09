from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.server import service_plugins
from app.schemas.gateway.service_plugin import PluginBinding, PluginDebuggerContribution
from app.schemas.internal_v2.node_debug import NodeDebugDebuggerContributionsDTO


@pytest.fixture
def debugger_discovery(tmp_path: Path):
    gateway_id = "gateway_" + "a" * 32
    workspace_id = str(uuid4())
    debugger = PluginDebuggerContribution(
        type="node",
        label="Node.js",
        configuration_schema={"type": "object", "properties": {"program": {"type": "string"}}},
        service_id="dap",
        dap_version=1,
        capabilities=["supportsConfigurationDoneRequest"],
    )
    service = SimpleNamespace(service_id="dap", service_type="debug", protocol="dap")
    receipt = SimpleNamespace(
        status="ready", workspace_id=workspace_id, gateway_id=gateway_id,
        instance_id="instance-original", services=[service],
    )
    process = SimpleNamespace(is_alive=lambda: True)
    slot = SimpleNamespace(
        binding=PluginBinding(
            package_id="boxteam.developer-tools", plugin_id="debugging",
            package_version="0.1.0", workspace_id=workspace_id, enabled=True,
        ),
        state="ready", process=process, receipt=receipt, stopped=asyncio.Event(),
        declaration=SimpleNamespace(
            runtime="node", contributes=SimpleNamespace(debuggers=[debugger]),
        ),
        revision="workspace-revision-7",
    )
    package = SimpleNamespace(digest="c" * 64)
    owner = SimpleNamespace(
        slots={("boxteam.developer-tools", "debugging", workspace_id): slot},
        packages=SimpleNamespace(get=lambda _package_id, _version: package),
    )
    registry = SimpleNamespace(
        gateway_id=gateway_id,
        resolve=lambda key: SimpleNamespace(connection_kind="local")
        if key == (gateway_id, workspace_id) else (_ for _ in ()).throw(LookupError("unknown workspace")),
    )
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(service_plugins.router)
    app.state.registry = registry
    app.state.service_plugins = owner
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    return app, gateway_id, workspace_id, debugger, package, slot


async def test_debugger_discovery_returns_only_current_ready_workspace_instances(
    debugger_discovery,
) -> None:
    app, gateway_id, workspace_id, debugger, package, slot = debugger_discovery
    headers = {
        "X-Local-Token": "local-token",
        "X-BoxTeam-Workspace-Id": workspace_id,
        "X-BoxTeam-Target-Gateway-Id": gateway_id,
        "X-Request-ID": "debugger-discovery-request",
    }
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 8000)),
        base_url="http://gateway.test",
    ) as client:
        response = await client.get(
            "/api/gateway/service-plugins/debugger-contributions",
            params={"workspace_id": workspace_id}, headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == "debugger-discovery-request"
    published = NodeDebugDebuggerContributionsDTO.model_validate(response.json()["data"])
    assert published.debuggers[0].registration_id
    assert response.json() == {
        "code": 0,
        "message": "ok",
        "data": {
            "gateway_id": gateway_id,
            "workspace_id": workspace_id,
            "debuggers": [{
                **debugger.model_dump(mode="json"),
                "registration_id": "node-debugger:instance-original:workspace-revision-7:node",
                "gateway_id": gateway_id,
                "workspace_id": workspace_id,
                "package_id": "boxteam.developer-tools",
                "plugin_id": "debugging",
                "package_version": "0.1.0",
                "package_digest": package.digest,
                "instance_id": "instance-original",
                "contract_version": 1,
                "declaration_revision": "workspace-revision-7",
            }],
        },
        "request_id": "debugger-discovery-request",
    }

    slot.state = "failed"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 8000)),
        base_url="http://gateway.test",
    ) as client:
        response = await client.get(
            "/api/gateway/service-plugins/debugger-contributions",
            params={"workspace_id": workspace_id}, headers=headers,
        )
    assert response.status_code == 200 and response.json()["data"]["debuggers"] == []


async def test_debugger_discovery_rejects_workspace_or_gateway_mismatch(
    debugger_discovery,
) -> None:
    app, gateway_id, workspace_id, _debugger, _package, _slot = debugger_discovery
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 8000)),
        base_url="http://gateway.test",
    ) as client:
        wrong_workspace = await client.get(
            "/api/gateway/service-plugins/debugger-contributions",
            params={"workspace_id": workspace_id},
            headers={
                "X-Local-Token": "local-token",
                "X-BoxTeam-Workspace-Id": str(uuid4()),
                "X-BoxTeam-Target-Gateway-Id": gateway_id,
            },
        )
        wrong_gateway = await client.get(
            "/api/gateway/service-plugins/debugger-contributions",
            params={"workspace_id": workspace_id},
            headers={
                "X-Local-Token": "local-token",
                "X-BoxTeam-Workspace-Id": workspace_id,
                "X-BoxTeam-Target-Gateway-Id": "gateway_" + "b" * 32,
            },
        )
    assert wrong_workspace.status_code == 403
    assert wrong_gateway.status_code == 409
