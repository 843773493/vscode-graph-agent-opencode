"""服务插件配置声明读取只依赖已安装包和配置读取权限。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from starlette.types import Message, Scope

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.server.service_plugins import router

GATEWAY_ID = "gateway_" + "a" * 32
PEER_GATEWAY_ID = "gateway_" + "b" * 32
WORKSPACE_ID = "12345678-1234-5678-1234-567812345678"
CONFIGURATION_SCHEMAS_PATH = "/api/gateway/service-plugins/configuration-schemas"


@pytest.fixture
async def plugin_configuration_gateway(
    tmp_path: Path,
) -> AsyncIterator[tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry]]:
    skill_sources = GatewaySkillSourceOwner(gateway_id=GATEWAY_ID, skills_root=tmp_path / "skills")
    await skill_sources.start()
    packages = PluginPackageStore(tmp_path / "packages")
    owner = GatewayPluginRuntime(
        home=tmp_path,
        gateway_id=GATEWAY_ID,
        packages=packages,
        settings=dict,
        workspace_root=lambda _: tmp_path,
        skill_sources=skill_sources,
    )
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=GATEWAY_ID)
    registry.upsert(WorkspaceTarget(
        gateway_id=GATEWAY_ID,
        workspace_id=WORKSPACE_ID,
        name="declared workspace",
        root_path=str(tmp_path / "workspace"),
        backend_url="",
        connection_kind="local",
    ), activate=False)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.state.service_plugins = owner
    app.state.registry = registry
    app.state.federation_policy_store = FederationPolicyStore(initial={"default_effect": "deny", "rules": [
        {"capability": "configuration_read", "effect": "allow",
         "principal_ref": principal_ref_for_gateway(PEER_GATEWAY_ID)},
        {"capability": "service_plugin_installation_manage", "effect": "deny"},
        {"capability": "service_plugin_runtime_manage", "effect": "deny"},
    ]})
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(
        kind="federation", peer_gateway_id=PEER_GATEWAY_ID,
    )
    try:
        yield app, owner, registry
    finally:
        await owner.close()
        await skill_sources.close()
        registry.close()


def _install(
    owner: GatewayPluginRuntime,
    tmp_path: Path,
    *,
    package_id: str,
    version: str,
    plugin_id: str,
    scope: str,
    configuration_schema: dict[str, object] | None,
) -> None:
    package_root = tmp_path / "sources" / f"{package_id}-{version}"
    package_root.mkdir(parents=True)
    if configuration_schema is None:
        contributes = {"skills": [{"id": "guide", "path": "guide"}]}
        requires = [{"extension_point": "skills", "version": 1}]
        skill = package_root / "guide" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text(
            "---\nname: plugin-guide\ndescription: Installed plugin guide\n---\n\nGuide.\n",
            encoding="utf-8",
        )
    else:
        contributes = {"configuration": configuration_schema}
        requires = [{"extension_point": "configuration", "version": 1}]
    manifest = {
        "manifest_version": 1,
        "package_id": package_id,
        "version": version,
        "name": package_id,
        "extension_contract_version": 1,
        "plugins": [{
            "id": plugin_id,
            "name": f"{plugin_id} {version}",
            "instance_scope": scope,
            "requires": requires,
            "contributes": contributes,
        }],
    }
    (package_root / "plugin.jsonc").write_text(json.dumps(manifest), encoding="utf-8")
    owner.packages.install_directory(package_root)


async def test_configuration_schemas_read_only_needs_configuration_read_and_never_starts_plugins(
    plugin_configuration_gateway: tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry],
    tmp_path: Path,
) -> None:
    app, owner, _ = plugin_configuration_gateway
    schema_v1 = {"type": "object", "properties": {"source": {"const": "v1"}}}
    schema_v2 = {"type": "object", "properties": {"source": {"const": "v2"}}}
    _install(owner, tmp_path, package_id="gateway.settings", version="1.0.0", plugin_id="settings",
        scope="gateway", configuration_schema=schema_v1)
    _install(owner, tmp_path, package_id="gateway.settings", version="2.0.0", plugin_id="settings",
        scope="gateway", configuration_schema=schema_v2)
    _install(owner, tmp_path, package_id="gateway.guide", version="1.0.0", plugin_id="guide",
        scope="gateway", configuration_schema=None)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(CONFIGURATION_SCHEMAS_PATH, json={"gateway_id": GATEWAY_ID})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["gateway_id"] == GATEWAY_ID
    assert data["workspace_id"] is None
    assert [
        (plugin["package_id"], plugin["package_version"], plugin["plugin_id"], plugin["configuration_schema"])
        for plugin in data["plugins"]
    ] == [
        ("gateway.settings", "1.0.0", "settings", schema_v1),
        ("gateway.settings", "2.0.0", "settings", schema_v2),
    ]
    assert owner.slots == {}


async def test_configuration_schemas_resolve_workspace_and_filter_scope_and_installed_versions(
    plugin_configuration_gateway: tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry],
    tmp_path: Path,
) -> None:
    app, owner, registry = plugin_configuration_gateway
    gateway_schema = {"type": "object", "properties": {"owner": {"const": "gateway"}}}
    workspace_v1 = {"type": "object", "properties": {"owner": {"const": "workspace-v1"}}}
    workspace_v2 = {"type": "object", "properties": {"owner": {"const": "workspace-v2"}}}
    _install(owner, tmp_path, package_id="gateway.settings", version="1.0.0", plugin_id="settings",
        scope="gateway", configuration_schema=gateway_schema)
    _install(owner, tmp_path, package_id="workspace.settings", version="1.0.0", plugin_id="settings",
        scope="workspace", configuration_schema=workspace_v1)
    _install(owner, tmp_path, package_id="workspace.settings", version="2.0.0", plugin_id="settings",
        scope="workspace", configuration_schema=workspace_v2)
    _install(owner, tmp_path, package_id="workspace.guide", version="1.0.0", plugin_id="guide",
        scope="workspace", configuration_schema=None)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(CONFIGURATION_SCHEMAS_PATH,
            json={"gateway_id": GATEWAY_ID, "workspace_id": WORKSPACE_ID})
        unregistered_workspace = await client.post(CONFIGURATION_SCHEMAS_PATH,
            json={"gateway_id": GATEWAY_ID, "workspace_id": "12345678-1234-5678-1234-567812345680"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["gateway_id"] == GATEWAY_ID and data["workspace_id"] == WORKSPACE_ID
    assert [
        (plugin["package_id"], plugin["package_version"], plugin["instance_scope"], plugin["configuration_schema"])
        for plugin in data["plugins"]
    ] == [
        ("workspace.settings", "1.0.0", "workspace", workspace_v1),
        ("workspace.settings", "2.0.0", "workspace", workspace_v2),
    ]
    assert unregistered_workspace.status_code == 409
    assert owner.slots == {}
    assert registry.resolve((GATEWAY_ID, WORKSPACE_ID)).desired_running is False


async def test_configuration_schemas_rejects_without_configuration_read(
    plugin_configuration_gateway: tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry],
) -> None:
    app, owner, _ = plugin_configuration_gateway
    app.state.federation_policy_store.publish({"default_effect": "deny", "rules": [
        {"capability": "service_plugin_installation_manage", "effect": "allow",
         "principal_ref": principal_ref_for_gateway(PEER_GATEWAY_ID)},
        {"capability": "service_plugin_runtime_manage", "effect": "allow",
         "principal_ref": principal_ref_for_gateway(PEER_GATEWAY_ID)},
    ]})

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(CONFIGURATION_SCHEMAS_PATH, json={"gateway_id": GATEWAY_ID})

    assert response.status_code == 403
    assert "configuration_read" in response.json()["detail"]
    assert owner.slots == {}


@pytest.mark.parametrize("capabilities", [
    ("configuration_read",),
    ("service_plugin_installation_manage",),
    ("service_plugin_runtime_manage",),
    ("configuration_read", "service_plugin_installation_manage", "service_plugin_runtime_manage"),
])
async def test_private_input_requires_configuration_write_before_creating_a_private_version(
    plugin_configuration_gateway: tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry],
    capabilities: tuple[str, ...],
) -> None:
    app, owner, _ = plugin_configuration_gateway
    app.state.federation_policy_store.publish({"default_effect": "deny", "rules": [
        {"capability": capability, "effect": "allow",
         "principal_ref": principal_ref_for_gateway(PEER_GATEWAY_ID)}
        for capability in capabilities
    ]})
    secret = "unauthorized-plugin-private-sentinel"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/gateway/service-plugins/private-inputs", json={
            "gateway_id": GATEWAY_ID,
            "package_id": "gateway.settings",
            "plugin_id": "settings",
            "package_version": "1.0.0",
            "source_key": "gateway_mutable_override",
            "parameter_pointer": "/credential",
            "value": secret,
        })

    assert response.status_code == 403
    assert "configuration_write" in response.json()["detail"]
    assert secret not in response.text
    assert not owner.private_inputs.directory.exists()
    assert owner.slots == {}


@pytest.mark.parametrize("workspace_id", [None, WORKSPACE_ID])
async def test_configuration_schema_events_follow_publication_and_revoke_without_runtime_access(
    plugin_configuration_gateway: tuple[FastAPI, GatewayPluginRuntime, GatewayWorkspaceRegistry],
    tmp_path: Path, workspace_id: str | None,
) -> None:
    app, owner, registry = plugin_configuration_gateway
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    payload = {"gateway_id": GATEWAY_ID, "workspace_id": workspace_id}
    path = CONFIGURATION_SCHEMAS_PATH + "/events"
    await incoming.put({"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False})
    scope: Scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"x-request-id", b"installed-schema-events")],
        "client": ("127.0.0.1", 12345), "server": ("test", 80)}
    running = asyncio.create_task(app(scope, incoming.get, outgoing.put))

    async def snapshot() -> dict[str, object]:
        message = await asyncio.wait_for(outgoing.get(), 3)
        assert message["type"] == "http.response.body"
        return json.loads(message["body"].decode().split("data: ", 1)[1])

    try:
        start = await asyncio.wait_for(outgoing.get(), 3)
        assert start["type"] == "http.response.start" and start["status"] == 200
        assert (b"x-request-id", b"installed-schema-events") in start["headers"]
        first = await snapshot()
        assert first["gateway_id"] == GATEWAY_ID and first["workspace_id"] == workspace_id
        assert first["plugins"] == []
        await asyncio.to_thread(_install, owner, tmp_path, package_id="events.settings", version="1.0.0",
            plugin_id="settings", scope="workspace" if workspace_id else "gateway",
            configuration_schema={"type": "object", "properties": {"mode": {"type": "boolean"}}})
        installed = await snapshot()
        plugins = installed["plugins"]
        assert isinstance(plugins, list)
        assert [(item["package_id"], item["package_version"]) for item in plugins] == [("events.settings", "1.0.0")]
        assert owner.slots == {}
        await asyncio.to_thread(owner.packages.uninstall, "events.settings", "1.0.0")
        assert (await snapshot())["plugins"] == []
        if workspace_id is None:
            app.state.federation_policy_store.publish({"default_effect": "deny", "rules": []})
        else:
            registry.remove((GATEWAY_ID, workspace_id))
        final = await asyncio.wait_for(outgoing.get(), 3)
        assert final == {"type": "http.response.body", "body": b"", "more_body": False}
        await asyncio.wait_for(running, 3)
        assert owner.packages._change_observers == ()
        assert registry._commit_observers == []
        assert owner.slots == {}
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
