"""Gateway 插件管理状态事件流。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.server.service_plugins import router
from app.schemas.gateway.service_plugin import PluginBinding, WorkspacePluginConfigurationDTO


class RuntimeStatusOwner:
    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id
        self.slots: dict[tuple[str, str, str | None], object] = {}
        self.changed = asyncio.Event()

    def publish(self) -> None:
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()


@pytest.fixture
def runtime_status_gateway(tmp_path: Path) -> tuple[FastAPI, RuntimeStatusOwner, SimpleNamespace, str, PluginBinding]:
    gateway_id = "gateway_" + "a" * 32
    workspace_id = "00000000-0000-4000-8000-000000000001"
    owner = RuntimeStatusOwner(gateway_id)
    binding = PluginBinding(package_id="proof.status", plugin_id="workspace", package_version="1.0.0",
        workspace_id=workspace_id, enabled=True)
    workspace = SimpleNamespace(
        configured_bindings=lambda: [binding],
        states={workspace_id: WorkspacePluginConfigurationDTO(workspace_id=workspace_id, state="waiting")},
    )
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.state.service_plugins = owner
    app.state.registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=gateway_id)
    app.state.gateway_config_reload = SimpleNamespace(config=SimpleNamespace(payload={}))
    app.state.workspace_plugin_bindings = workspace
    app.state.federation_policy_store = FederationPolicyStore(initial={"default_effect": "allow", "rules": []})
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    return app, owner, workspace, workspace_id, binding


async def test_runtime_status_events_send_complete_workspace_snapshot_and_wake_on_change(
    runtime_status_gateway: tuple[FastAPI, RuntimeStatusOwner, SimpleNamespace, str, PluginBinding],
) -> None:
    app, owner, workspace, workspace_id, binding = runtime_status_gateway
    incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": json.dumps({"gateway_id": owner.gateway_id}).encode(),
                        "more_body": False})
    scope: dict[str, object] = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/gateway/service-plugins/status/events",
        "raw_path": b"/api/gateway/service-plugins/status/events", "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"x-request-id", b"runtime-status-request")],
        "client": ("127.0.0.1", 12345), "server": ("test", 80),
    }
    running = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["type"] == "http.response.start" and start["status"] == 200
        assert dict(start["headers"])[b"x-request-id"] == b"runtime-status-request"

        initial = await asyncio.wait_for(outgoing.get(), 3)
        assert initial["type"] == "http.response.body"
        first_frame = initial["body"].decode()
        assert first_frame.startswith("event: snapshot\ndata: ")
        first = json.loads(first_frame.removeprefix("event: snapshot\ndata: ").strip())
        assert first["configured_bindings"] == [binding.model_dump(mode="json")]
        assert first["workspace_configurations"] == [{
            "workspace_id": workspace_id, "state": "waiting", "revision": None, "error": None,
        }]

        workspace.states[workspace_id] = WorkspacePluginConfigurationDTO(
            workspace_id=workspace_id, state="ready", revision="workspace-revision",
        )
        owner.publish()
        updated = await asyncio.wait_for(outgoing.get(), 3)
        assert updated["type"] == "http.response.body"
        second_frame = updated["body"].decode()
        assert second_frame.startswith("event: snapshot\ndata: ")
        second = json.loads(second_frame.removeprefix("event: snapshot\ndata: ").strip())
        assert second["workspace_configurations"] == [{
            "workspace_id": workspace_id, "state": "ready", "revision": "workspace-revision", "error": None,
        }]
    finally:
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(running, 3)
