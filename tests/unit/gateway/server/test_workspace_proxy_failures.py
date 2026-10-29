"""上游异常保留原响应与租约清理，同时提供 request_id 关联的完整日志。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.auxiliary_proxy import (
    proxy_attach_frontend,
    proxy_auxiliary_http,
)
from app.gateway.auxiliary_proxy import router as auxiliary_router
from app.gateway.control.user_access import UserAccessContext
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.workspace_proxy import (
    _proxy_workspace_request,
    verify_user_access_for_proxy,
)
from app.gateway.server.workspace_proxy import router as workspace_router
from tests.unit.gateway.server.test_workspace_proxy_route_reference import (
    _hermetic_gateway_env as _hermetic_gateway_env,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_workspace_proxy_route_reference import _make_request


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(WorkspaceTarget(
        workspace_id="diagnostic-target", name="上游错误诊断",
        root_path=str(tmp_path / "workspace"), backend_url="http://127.0.0.1:41001",
        connection_kind="local", backend_workspace_id="00000000-0000-4000-8000-000000000001",
    ), runtime=WorkspaceRuntime(service_urls={
        "workspace_api": "http://127.0.0.1:41001", "terminal_manager": "http://127.0.0.1:41002",
    }), activate=False)
    return registry


@pytest.mark.parametrize("entry,error_type,status,stage", [
    (entry, error_type, status, stage)
    for entry in ("workspace", "auxiliary", "attach")
    for error_type, status, stage in [
        (httpx.ConnectError, 502, "upstream_request"),
        (httpx.ReadError, 502, "upstream_request"),
        (httpx.RemoteProtocolError, 502, "upstream_request"),
        (httpx.PoolTimeout, 502, "upstream_request"),
        (TimeoutError, 504, "upstream_headers"),
    ]
    if entry != "attach" or error_type is not TimeoutError
])
@pytest.mark.parametrize("native_path", [
    "/private/proxy-native-marker/socket", r"C:\private\proxy-native-marker\socket",
    r"\\server\proxy-native-marker\socket",
])
async def test_upstream_failure_logs_cause_without_retry_or_route_leak(
    registry: GatewayWorkspaceRegistry, caplog: pytest.LogCaptureFixture,
    entry: str, error_type: type[Exception], status: int, stage: str, native_path: str,
) -> None:
    cause = OSError(111, "测试底层连接诊断")
    failure = error_type(f"测试上游请求故障: {native_path}")
    calls = 0

    async def fail_upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["X-Request-ID"] == "req_route_ref"
        raise failure from cause

    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_upstream)) as client:
        app = FastAPI()
        app.state.registry = registry
        app.state.http_client = client
        app.state.streaming_http_client = client
        app.state.attach_frontend_urls = {"terminal": "http://127.0.0.1:41003"}
        request = _make_request(app, method="GET", path="/api/v1/jobs/job_1", workspace_id="diagnostic-target")
        request._body = b""
        with pytest.raises(HTTPException) as caught:
            if entry == "workspace":
                await _proxy_workspace_request("jobs/job_1", request, auth=None, user_access=None, include_credentials=False)
            elif entry == "auxiliary":
                await proxy_auxiliary_http(
                    "diagnostic-target", "terminal-manager", "health", request, GatewayAuthContext(kind="local"),
                )
            else:
                await proxy_attach_frontend("terminal", "index.html", request)

    assert caught.value.status_code == status
    assert caught.value.__cause__ is failure and failure.__cause__ is cause
    assert native_path not in str(caught.value.detail) and "proxy-native-marker" not in str(caught.value.detail)
    if issubclass(error_type, httpx.RequestError):
        assert f"error_type={error_type.__name__}" in str(caught.value.detail)
    assert calls == 1, "普通请求不因诊断日志而自动重试"
    assert registry.route_reference_counts("diagnostic-target") == (0, 0)
    registry._assert_route_references_drained("diagnostic-target")
    logger_name = "app.gateway.server.workspace_proxy" if entry == "workspace" else "app.gateway.auxiliary_proxy"
    diagnostics = [record for record in caplog.records if record.name == logger_name]
    assert len(diagnostics) == 1
    assert diagnostics[0].exc_info is not None and diagnostics[0].exc_info[1] is failure
    for expected in ("req_route_ref", "GET", f"stage={stage}", str(cause), str(failure)):
        assert expected in caplog.text
    assert ("kind=terminal" if entry == "attach" else "diagnostic-target") in caplog.text


@pytest.mark.parametrize("path", [
    "/api/v1/jobs/job_1", "/api/gateway/workspaces/diagnostic-target/terminal-manager/health",
    "/api/gateway/attach/terminal/index.html",
])
@pytest.mark.parametrize("native_path", [
    "/private/proxy-native-marker/socket", r"C:\private\proxy-native-marker\socket",
    r"\\server\proxy-native-marker\socket",
])
async def test_proxy_routes_serialize_safe_failure_and_allow_explicit_retry(
    registry: GatewayWorkspaceRegistry, caplog: pytest.LogCaptureFixture, path: str, native_path: str,
) -> None:
    """真实ASGI路由和序列化；认证与上游网络是显式单元测试边界。"""
    failure = httpx.ConnectError(f"连接底层文件失败: {native_path}")
    calls: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise failure
        return httpx.Response(200, json={"ok": True, "request_id": request.headers["X-Request-ID"]})

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.include_router(workspace_router)
    app.include_router(auxiliary_router)
    # 单测在认证依赖边界提供已验证上下文，不替生产创建业务或资源状态。
    access = UserAccessContext(
        kind="guest", user_id=None, access_session_id="proxy-unit", lease_generation=1,
        invalidated=asyncio.Event(),
    )
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    app.dependency_overrides[verify_user_access_for_proxy] = lambda: access
    app.state.registry = registry
    app.state.attach_frontend_urls = {"terminal": "http://127.0.0.1:41003"}
    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False) as transport:
        app.state.http_client = transport
        app.state.streaming_http_client = transport
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway.test", trust_env=False) as client:
            failed = await client.get(path, headers={
                "X-Request-ID": "proxy-serialized-failure", "X-BoxTeam-Workspace-Id": "diagnostic-target",
            })
            assert failed.status_code == 502, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"] == "proxy-serialized-failure"
            assert "error_type=ConnectError" in failed.json()["detail"]
            assert "proxy-native-marker" not in failed.text and native_path not in failed.text
            assert "http://127.0.0.1:4100" not in failed.text
            assert len(calls) == 1 and registry.route_reference_counts("diagnostic-target") == (0, 0)
            recovered = await client.get(path, headers={
                "X-Request-ID": "proxy-serialized-retry", "X-BoxTeam-Workspace-Id": "diagnostic-target",
            })
            assert recovered.status_code == 200 and recovered.json() == {"ok": True, "request_id": "proxy-serialized-retry"}
            assert recovered.headers["X-Request-ID"] == "proxy-serialized-retry"
            assert len(calls) == 2 and registry.route_reference_counts("diagnostic-target") == (0, 0)
    assert [call.headers["X-Request-ID"] for call in calls] == ["proxy-serialized-failure", "proxy-serialized-retry"]
    diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is failure]
    assert len(diagnostics) == 1 and "proxy-serialized-failure" in diagnostics[0].getMessage()
    assert native_path in caplog.text


@pytest.mark.parametrize("backend_identity", [
    "/private/backend-identity-marker/workspace", r"C:\private\backend-identity-marker\workspace",
    r"\\server\backend-identity-marker\workspace", "",
    "ABCDEF00-0000-4000-8000-000000000001",
])
@pytest.mark.parametrize("canonical", [False, True])
async def test_backend_identity_header_rejects_before_target_lookup_and_recovers(
    registry: GatewayWorkspaceRegistry, monkeypatch: pytest.MonkeyPatch,
    backend_identity: str, canonical: bool,
) -> None:
    backend_id = "00000000-0000-4000-8000-000000000001"
    path = f"/api/v1/workspaces/{backend_id}/workspace" if canonical else "/api/v1/workspace"
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.exception_handler(HTTPException)(gateway_http_exception_handler)
    app.include_router(workspace_router)
    app.state.registry = registry
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    app.dependency_overrides[verify_user_access_for_proxy] = lambda: None
    lookups: list[str | None] = []
    forwarded: list[httpx.Request] = []
    original_resolve = registry.resolve

    def resolve(workspace_id: str | None = None) -> WorkspaceTarget:
        lookups.append(workspace_id)
        return original_resolve(workspace_id)

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"request_id": request.headers["X-Request-ID"], "data": {"ok": True}})

    monkeypatch.setattr(registry, "resolve", resolve)
    original_targets = registry.targets()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False) as transport:
        app.state.http_client = app.state.streaming_http_client = transport
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway.test", trust_env=False) as client:
            headers = {"X-Request-ID": "backend-header-rejected", "X-BoxTeam-Workspace-Id": "diagnostic-target",
                       "X-BoxTeam-Backend-Workspace-Id": backend_identity}
            rejected = await client.get(path, headers=headers)
            assert rejected.status_code == 400, rejected.text
            assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == headers["X-Request-ID"]
            assert "backend-identity-marker" not in rejected.text
            expected = "必须使用标准 UUID 文本" if backend_identity.startswith("ABCDEF") else "必须是标准 UUID"
            assert expected in rejected.json()["detail"]
            assert lookups == [] and forwarded == []
            assert registry.targets() == original_targets
            assert registry.route_reference_counts("diagnostic-target") == (0, 0)

            headers["X-BoxTeam-Backend-Workspace-Id"] = "00000000-0000-4000-8000-000000000002"
            conflict = await client.get(path, headers=headers)
            assert conflict.status_code == 400 and "不一致" in conflict.json()["detail"]
            assert forwarded == []
            headers["X-BoxTeam-Backend-Workspace-Id"] = backend_id
            headers["X-Request-ID"] = "backend-header-recovered"
            recovered = await client.get(path, headers=headers)
            assert recovered.status_code == 200 and recovered.json()["data"] == {"ok": True}
            assert recovered.headers["X-Request-ID"] == recovered.json()["request_id"] == headers["X-Request-ID"]

    assert len(forwarded) == 1
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == backend_id
    assert "X-BoxTeam-Backend-Workspace-Id" not in forwarded[0].headers
    assert registry.targets() == original_targets
    assert registry.route_reference_counts("diagnostic-target") == (0, 0)
