"""精确插件业务代理的失败归属与零重放。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from httpx import ASGITransport
from pydantic import Secret

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.main import gateway_http_exception_handler
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.plugins.workspaces import WorkspacePluginBindings
from app.gateway.proxy_upstream import UpstreamResponseBody
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.server import service_plugins as management_plugins
from app.gateway.server.service_plugin_proxy import (
    _ServiceResponse,
    _ServiceRoute,
    router,
)
from app.schemas.gateway.plugin_installation import (
    FrozenPluginInstallArtifact,
    InstallPluginRequest,
    PluginInstallOperationDTO,
)
from app.schemas.gateway.plugin_private_inputs import PluginPrivateInputWriteRequest
from configs.plugin_parameters import PluginPrivateInputReference


@pytest.fixture
async def plugin_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[FastAPI, list[httpx.Request]]]:
    calls: list[httpx.Request] = []
    stopped = asyncio.Event()
    mode = getattr(request, "param", "disconnect")

    async def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if mode != "disconnect":
            stopped.set()
        if mode == "unexpected":
            raise ExceptionGroup("传输与未知业务错误", [
                httpx.ConnectError("具名插件连接已退出", request=request), ValueError("业务错误不能归为断网"),
            ])
        raise httpx.ConnectError("具名插件连接已退出", request=request)

    skill_sources = GatewaySkillSourceOwner(gateway_id="gateway_" + "a" * 32, skills_root=tmp_path / "skills")
    await skill_sources.start()
    owner = GatewayPluginRuntime(home=tmp_path, gateway_id="gateway_" + "a" * 32,
        packages=PluginPackageStore(tmp_path / "packages"), settings=dict, workspace_root=lambda _: tmp_path,
        skill_sources=skill_sources)
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    monkeypatch.setattr(owner, "service", lambda *args, **kwargs: (
        "http://127.0.0.1:49152/services/echo", "private", stopped, "a" * 64))
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.include_router(router)
    app.state.service_plugins = owner
    from app.gateway.registry import GatewayWorkspaceRegistry
    app.state.registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=owner.gateway_id)
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    try:
        yield app, calls
    finally:
        await owner.close()
        await skill_sources.close()


@pytest.mark.parametrize("plugin_proxy", ["disconnect", "disconnect-and-retire"], indirect=True)
async def test_service_connect_failure_is_attributed_without_replay(
    plugin_proxy: tuple[FastAPI, list[httpx.Request]], caplog: pytest.LogCaptureFixture,
) -> None:
    app, calls = plugin_proxy
    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/write?package_version=1.0.0",
            json={"value": 1}, headers={"X-Request-ID": "plugin-original-request"})
    assert response.status_code == 502
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "plugin-original-request"
    assert "instance_id=original" in response.json()["detail"]
    assert len(calls) == 1
    assert calls[0].headers["X-Request-ID"] == "plugin-original-request"
    assert calls[0].headers["X-Local-Token"] == "private"
    assert "具名插件连接已退出" in caplog.text


@pytest.mark.parametrize("plugin_proxy", ["unexpected"], indirect=True)
async def test_service_unexpected_group_is_not_hidden_as_connection_failure(
    plugin_proxy: tuple[FastAPI, list[httpx.Request]], caplog: pytest.LogCaptureFixture,
) -> None:
    app, calls = plugin_proxy
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/read?package_version=1.0.0")
    assert response.status_code == 500
    assert "业务错误不能归为断网" in caplog.text
    assert len(calls) == 1


async def test_install_authorization_is_decided_before_operation_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: list[str] = []

    class Installations:
        async def start(self, payload: InstallPluginRequest) -> PluginInstallOperationDTO:
            accepted.append(payload.operation_id)
            return PluginInstallOperationDTO(gateway_id=payload.gateway_id, operation_id=payload.operation_id,
                status="pending", artifact=FrozenPluginInstallArtifact(source_kind="local_zip",
                    package_id="demo.plugin", package_version="1.0.0", digest="sha256:" + "a" * 64,
                    path=payload.path))

    request = cast(Request, SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(plugin_installations=Installations())),
        state=SimpleNamespace(request_id="install-request"),
    ))
    monkeypatch.setattr(management_plugins, "relay", AsyncMock(return_value=None))
    checks = 0

    def authorize(*_args: object) -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise HTTPException(403, "授权已撤销")

    monkeypatch.setattr(management_plugins, "require_federation_capability", authorize)
    payload = InstallPluginRequest(gateway_id="gateway_" + "a" * 32, operation_id="install-1",
        path="/tmp/plugin.zip")
    result = await management_plugins.install(payload, request, GatewayAuthContext(kind="local"), Mock())

    assert result.data is not None
    assert result.data.operation_id == "install-1"
    assert accepted == ["install-1"]
    assert checks == 1


async def test_private_input_authorization_is_decided_before_secret_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_id = "gateway_" + "a" * 32
    written: list[str] = []
    reference = PluginPrivateInputReference(gateway_id=gateway_id, package_id="demo.plugin",
        plugin_id="demo", parameter_pointer="/token", version="a" * 32)

    class PrivateInputs:
        def write(self, **_: object) -> PluginPrivateInputReference:
            written.append("secret")
            return reference

    declaration = SimpleNamespace(contributes=SimpleNamespace(configuration={}))
    owner = SimpleNamespace(gateway_id=gateway_id, private_inputs=PrivateInputs(), declaration=lambda _: declaration)
    document = SimpleNamespace(writable=True, source_effective={})
    request = cast(Request, SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(gateway_config_reload=SimpleNamespace(
            get_source_document=lambda **_: document))),
        state=SimpleNamespace(request_id="private-input-request"),
    ))
    payload = PluginPrivateInputWriteRequest(gateway_id=gateway_id, package_id="demo.plugin", plugin_id="demo",
        package_version="1.0.0", source_key="gateway_user_override", parameter_pointer="/token",
        value=Secret("secret"))
    monkeypatch.setattr(management_plugins, "relay", AsyncMock(return_value=None))
    monkeypatch.setattr(management_plugins, "plugin_parameters", lambda _: {})
    monkeypatch.setattr(management_plugins, "validate_private_input", lambda *args: None)

    async def run_sync(callback, **_kwargs):
        return callback()

    monkeypatch.setattr(management_plugins, "run_sync_shielded", run_sync)
    checks = 0

    def authorize(*_args: object) -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise HTTPException(403, "授权已撤销")

    monkeypatch.setattr(management_plugins, "require_federation_capability", authorize)
    result = await management_plugins.write_gateway_private_input(payload, request, Response(),
        GatewayAuthContext(kind="local"), cast(GatewayPluginRuntime, owner))

    assert result.data == reference
    assert written == ["secret"]
    assert checks == 1


async def test_service_path_cannot_escape_declared_service(plugin_proxy: tuple[FastAPI, list[httpx.Request]]) -> None:
    app, calls = plugin_proxy
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/%252e%252e/internal?package_version=1.0.0")
    assert response.status_code == 422
    assert calls == []


async def test_native_service_publishes_installed_package_identity(
    plugin_proxy: tuple[FastAPI, list[httpx.Request]],
) -> None:
    app, _ = plugin_proxy
    owner = app.state.service_plugins
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"X-BoxTeam-Plugin-Package-Digest": "forged", "X-BoxTeam-Plugin-Instance-Id": "forged"},
        json={"result": "ready"})))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/read?package_version=1.0.0")
    assert response.status_code == 200
    assert response.headers["X-BoxTeam-Plugin-Package-Digest"] == "a" * 64
    assert response.headers["X-BoxTeam-Plugin-Instance-Id"] == "original"
    assert response.headers["X-BoxTeam-Plugin-Gateway-Id"] == owner.gateway_id


@pytest.mark.parametrize(("workspace_id", "binding_workspace", "expected_status"), [
    ("00000000-0000-4000-8000-000000000001", None, 502),
    ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000001", 502),
    ("invalid", None, 422),
    ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", 422),
])
async def test_gateway_service_keeps_caller_workspace_separate_from_instance(
    plugin_proxy: tuple[FastAPI, list[httpx.Request]], monkeypatch: pytest.MonkeyPatch,
    workspace_id: str, binding_workspace: str | None, expected_status: int,
) -> None:
    app, calls = plugin_proxy
    registry = app.state.registry
    monkeypatch.setattr(registry, "resolve", lambda key: WorkspaceTarget(gateway_id=registry.gateway_id,
        workspace_id=key[1], name="caller", root_path="/caller", backend_url="", connection_kind="local"))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/read",
            params={"package_version": "1.0.0", **({"workspace_id": binding_workspace} if binding_workspace else {})},
            headers={"X-BoxTeam-Workspace-Id": workspace_id})
    assert response.status_code == expected_status
    if expected_status == 502:
        assert calls[0].headers.get_list("X-BoxTeam-Workspace-Id") == [workspace_id]
    else:
        assert calls == []


@pytest.mark.parametrize("instance_id", ["", "original", "wrong-instance"])
async def test_relay_preserves_native_service_identity(instance_id: str) -> None:
    request = Request({"type": "http", "headers": []})
    response = httpx.Response(200, headers={"X-BoxTeam-Plugin-Gateway-Id": "native-gateway",
        "X-BoxTeam-Plugin-Instance-Id": "original", "X-BoxTeam-Plugin-Package-Digest": "b" * 64})
    body = UpstreamResponseBody(response)
    route = _ServiceRoute(httpx.URL("http://native/services/mcp"), {}, asyncio.Event(),
        instance_id, "native-gateway", True)
    try:
        if instance_id == "wrong-instance":
            with pytest.raises(ValueError, match="原始实例身份"):
                _ServiceResponse(body, request=request, auth=GatewayAuthContext(kind="local"), route=route, response=response)
        else:
            result = _ServiceResponse(body, request=request, auth=GatewayAuthContext(kind="local"), route=route, response=response)
            assert result.headers["X-BoxTeam-Plugin-Instance-Id"] == "original"
            assert result.headers["X-BoxTeam-Plugin-Gateway-Id"] == "native-gateway"
            assert result.headers["X-BoxTeam-Plugin-Package-Digest"] == "b" * 64
    finally:
        await body.aclose()


async def test_creation_handoff_observes_completed_start_before_marking_signature_processed() -> None:
    """启动持拓扑锁时收到创建通知，不能把尚未交接的启动完成误记为已处理。"""
    target = WorkspaceTarget(gateway_id="gateway_" + "a" * 32,
        workspace_id="12345678-1234-5678-1234-567812345678", name="new", root_path="/new",
        backend_url="", connection_kind="local", managed=True,
        plugin_creation={"intent_id": "create-original", "revision": "template-v1", "bindings": []})
    registry = Mock(spec=GatewayWorkspaceRegistry)
    registry.gateway_id = target.gateway_id
    registry.backend_topology_lock = asyncio.Lock()
    registry.targets.return_value = (target,)
    registry.backend_owner.return_value = None
    runtime = Mock(spec=GatewayPluginRuntime)
    runtime.slots = {}
    coordinator = WorkspacePluginBindings(registry, runtime)
    coordinator._task = asyncio.current_task()

    async def initialized(current, creation):
        assert current.desired_running
        assert creation.intent_id == "create-original"
        current.plugin_creation = None

    async def refreshed(current):
        assert current.plugin_creation is None

    coordinator._initialize_creation = AsyncMock(side_effect=initialized)
    coordinator._refresh = AsyncMock(side_effect=refreshed)
    async with registry.backend_topology_lock:
        reconciliation = asyncio.create_task(coordinator.reconcile())
        await asyncio.sleep(0)
        target.desired_running = True
        target.backend_url = "http://127.0.0.1:49152"
    await reconciliation
    coordinator._initialize_creation.assert_awaited_once()
    coordinator._refresh.assert_awaited_once()
