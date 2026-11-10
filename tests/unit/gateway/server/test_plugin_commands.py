"""业务权限在发起调用前核验，安装和运行管理权限不代授命令执行。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime, PluginSlot
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.server.plugin_commands import router
from app.schemas.gateway.plugin_commands import PluginCommandRegistration
from app.schemas.gateway.service_plugin import (
    PluginBinding,
    PluginInstanceStatus,
    ServicePluginManifest,
)


@pytest.fixture
async def command_gateway(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, list[httpx.Request], dict[str, object]]]:
    calls: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"instance_id": "original", "call_id": "cancel", "accepted": False})

    gateway_id = "gateway_" + "a" * 32
    skill_sources = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=tmp_path / "skills")
    await skill_sources.start()
    owner = GatewayPluginRuntime(home=tmp_path, gateway_id=gateway_id,
        packages=PluginPackageStore(tmp_path / "packages"), settings=dict, workspace_root=lambda _: tmp_path,
        skill_sources=skill_sources)
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    declaration = ServicePluginManifest.model_validate({"id": "command", "name": "Command", "instance_scope": "gateway",
        "runtime": "python", "entrypoint": ["entry.py"], "management_protocol_version": 1,
        "requires": [{"extension_point": "commands", "version": 1}], "contributes": {"commands": [{
            "id": "write", "title": "Write", "handler": "write", "arguments_schema": {}, "result_schema": {},
            "required_capabilities": ["file_write"]}]}})
    binding = PluginBinding(package_id="proof.commands", package_version="1.0.0", plugin_id="command", enabled=True)
    slot = PluginSlot(binding=binding, revision="revision", declaration=declaration, gateway_id=gateway_id,
        package_digest="a" * 64,
        base_url="http://127.0.0.1:12345", token="private", state="ready",
        receipt=PluginInstanceStatus.model_validate({"gateway_id": gateway_id,
            **binding.model_dump(exclude={"enabled"}), "instance_scope": "gateway", "instance_id": "original",
            "management_protocol_version": 1, "extension_contract_version": 1, "status": "ready",
            "process_id": 1, "services": [], "capabilities": ["commands"]}),
        commands={"write": PluginCommandRegistration(command_id="write", handler="write", registration_id="registration")})
    owner.slots[owner.key(binding)] = slot
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.state.service_plugins = owner
    app.state.registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=gateway_id)
    app.state.federation_policy_store = FederationPolicyStore(initial={"default_effect": "allow", "rules": [
        {"capability": "file_write", "effect": "deny"},
        {"capability": "service_plugin_runtime_manage", "effect": "deny"}]})
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="federation", peer_gateway_id="gateway_" + "b" * 32)
    payload: dict[str, object] = {"gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "1.0.0",
        "plugin_id": "command", "instance_id": "original", "registration_id": "registration",
        "command_id": "write", "call_id": "cancel"}
    try:
        yield app, calls, payload
    finally:
        await owner.close()
        await skill_sources.close()


async def test_command_requires_declared_business_permission_before_upstream(
    command_gateway: tuple[FastAPI, list[httpx.Request], dict[str, object]],
) -> None:
    app, calls, payload = command_gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/gateway/service-plugins/commands/cancel", json=payload)
        assert response.status_code == 403 and "file_write" in response.text
        assert calls == []
        app.state.federation_policy_store.publish({"default_effect": "allow", "rules": [
            {"capability": "service_plugin_runtime_manage", "effect": "deny"}]})
        response = await client.post("/api/gateway/service-plugins/commands/cancel", json=payload,
                                     headers={"X-Request-ID": "command-business-only"})
        assert response.status_code == 200
        assert len(calls) == 1 and calls[0].headers["X-Request-ID"] == "command-business-only"


async def test_command_publications_use_business_read_and_current_permissions(
    command_gateway: tuple[FastAPI, list[httpx.Request], dict[str, object]],
) -> None:
    app, calls, payload = command_gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/gateway/service-plugins/commands/publications",
            json={"gateway_id": payload["gateway_id"]}, headers={"X-Request-ID": "publication-business-only"})
        assert response.status_code == 200 and response.json()["request_id"] == "publication-business-only"
        publication = response.json()["data"]["publications"][0]
        assert publication["commands"] == [] and publication["registrations"] == []
        app.state.federation_policy_store.publish({"default_effect": "allow", "rules": [
            {"capability": "service_plugin_runtime_manage", "effect": "deny"}]})
        response = await client.post("/api/gateway/service-plugins/commands/publications",
                                     json={"gateway_id": payload["gateway_id"]})
        assert response.status_code == 200
        publication = response.json()["data"]["publications"][0]
        assert [item["id"] for item in publication["commands"]] == ["write"]
        assert publication["registrations"][0]["registration_id"] == "registration"
        assert publication["instance_id"] == "original"
        assert not ({"parameters", "token", "base_url"} & publication.keys())
        app.state.federation_policy_store.publish({"rules": [{"capability": "read", "effect": "deny"}]})
        response = await client.post("/api/gateway/service-plugins/commands/publications",
                                     json={"gateway_id": payload["gateway_id"]})
        assert response.status_code == 403 and "read" in response.text
        assert calls == []


async def test_publication_subscription_updates_permissions_and_finishes_revoked_stream(
    command_gateway: tuple[FastAPI, list[httpx.Request], dict[str, object]],
) -> None:
    app, calls, payload = command_gateway
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": json.dumps({"gateway_id": payload["gateway_id"]}).encode(),
                        "more_body": False})
    scope: Scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/gateway/service-plugins/commands/events",
        "raw_path": b"/api/gateway/service-plugins/commands/events", "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"x-request-id", b"publication-permissions")],
        "client": ("127.0.0.1", 12345), "server": ("test", 80)}
    running = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["type"] == "http.response.start" and start["status"] == 200
        first = await asyncio.wait_for(outgoing.get(), 3)
        assert first["type"] == "http.response.body"
        initial = json.loads(first["body"].decode().split("data: ", 1)[1])
        assert initial["publications"][0]["registrations"] == []
        app.state.federation_policy_store.publish({"rules": [{"capability": "service_plugin_runtime_manage", "effect": "deny"}]})
        updated = await asyncio.wait_for(outgoing.get(), 3)
        assert updated["type"] == "http.response.body"
        snapshot = json.loads(updated["body"].decode().split("data: ", 1)[1])
        assert snapshot["publications"][0]["registrations"][0]["registration_id"] == "registration"
        app.state.federation_policy_store.publish({"rules": [{"capability": "read", "effect": "deny"}]})
        final = await asyncio.wait_for(outgoing.get(), 3)
        assert final == {"type": "http.response.body", "body": b"", "more_body": False}
        await asyncio.wait_for(running, 3)
        assert calls == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
