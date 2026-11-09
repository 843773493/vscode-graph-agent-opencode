from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext
from app.gateway.server import service_plugin_proxy
from app.schemas.gateway.service_plugin import PluginDebuggerContribution


@pytest.fixture
def debug_runtime_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace_id = str(uuid4())
    debugger = PluginDebuggerContribution(
        type="node",
        label="Node.js",
        configuration_schema={"type": "object", "properties": {"program": {"type": "string"}}},
        service_id="dap",
        dap_version=1,
        capabilities=["supportsConfigurationDoneRequest"],
    )
    slot = SimpleNamespace(
        revision="revision-1",
        declaration=SimpleNamespace(
            runtime="node",
            contributes=SimpleNamespace(debuggers=[debugger]),
        ),
    )
    package = SimpleNamespace(
        digest="b" * 64,
        manifest=SimpleNamespace(extension_contract_version=1),
    )
    owner = SimpleNamespace(
        key=lambda _binding: ("boxteam.developer-tools", "debugging", workspace_id),
        slots={("boxteam.developer-tools", "debugging", workspace_id): slot},
        packages=SimpleNamespace(get=lambda _package_id, _version: package),
    )
    stopped = asyncio.Event()
    route = SimpleNamespace(
        url=httpx.URL("http://127.0.0.1:49152/services/dap/__debug_runtime__"),
        headers={"X-Local-Token": "private-runtime-token"},
        stopped=stopped,
        instance_id="instance-original",
        gateway_id="gateway_original",
        remote=False,
    )

    @contextmanager
    def resolved_route(*_args, **_kwargs):
        yield route

    monkeypatch.setattr(service_plugin_proxy, "_route", resolved_route)
    monkeypatch.setattr(
        service_plugin_proxy,
        "verify_gateway_access",
        lambda **_kwargs: GatewayAuthContext(kind="local"),
    )

    forwarded: list[str] = []
    forwarded_event = threading.Event()
    release_upstream = asyncio.Event()

    class Upstream:
        async def send(self, message: str) -> None:
            forwarded.append(message)
            forwarded_event.set()

        async def __aiter__(self):
            await release_upstream.wait()
            if False:
                yield ""

    upstream = Upstream()

    class UpstreamContext:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_args):
            release_upstream.set()

    def fake_connect(url: str, **kwargs):
        return UpstreamContext()

    monkeypatch.setattr(service_plugin_proxy, "connect", fake_connect)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(service_plugin_proxy.router)
    app.state.service_plugins = owner

    return app, workspace_id, debugger, package, forwarded, forwarded_event, route


def test_debug_runtime_route_sends_exact_ready_identity_and_relays_control_frames(
    debug_runtime_proxy,
) -> None:
    app, workspace_id, debugger, package, forwarded, forwarded_event, route = debug_runtime_proxy
    path = (
        "/api/gateway/service-plugins/boxteam.developer-tools/debugging/instances/"
        f"{route.instance_id}/debug-runtime?package_version=0.1.0&workspace_id={workspace_id}"
    )
    request = {
        "version": 1,
        "type": "request",
        "request_id": "request-1",
        "operation_id": "operation-1",
        "run_id": "run-1",
        "operation": "launch",
        "payload": {"node_executable": "/target/node", "script_path": "/target/main.js",
                    "args": [], "working_directory": "/target", "break_on_start": True},
    }
    with TestClient(app) as client, client.websocket_connect(path, headers={
        "X-Local-Token": "local-auth",
        "X-BoxTeam-Workspace-Id": workspace_id,
    }) as socket:
        ready = socket.receive_json()
        assert ready == {
            "version": 1,
            "type": "ready",
            "gateway_id": route.gateway_id,
            "workspace_id": workspace_id,
            "package_id": "boxteam.developer-tools",
            "plugin_id": "debugging",
            "package_version": "0.1.0",
            "package_digest": package.digest,
            "extension_contract_version": 1,
            "declaration_revision": "revision-1",
            "instance_id": route.instance_id,
            "service_id": "dap",
            "contract_version": 1,
            "debuggers": [debugger.model_dump(mode="json")],
        }
        socket.send_json(request)
        assert forwarded_event.wait(2)
        assert [json.loads(frame) for frame in forwarded] == [request]
        assert route.headers["X-Local-Token"] == "private-runtime-token"


def test_debug_runtime_route_rejects_query_credentials_before_opening_an_upstream(
    debug_runtime_proxy,
) -> None:
    app, workspace_id, _debugger, _package, forwarded, _event, route = debug_runtime_proxy
    path = (
        "/api/gateway/service-plugins/boxteam.developer-tools/debugging/instances/"
        f"{route.instance_id}/debug-runtime?package_version=0.1.0&workspace_id={workspace_id}&token=secret"
    )
    with pytest.raises(WebSocketDenialResponse) as denied, TestClient(app) as client, client.websocket_connect(
        path, headers={
            "X-Local-Token": "local-auth",
            "X-BoxTeam-Workspace-Id": workspace_id,
        },
    ):
        pass
    assert denied.value.status_code == 403
    assert forwarded == []
