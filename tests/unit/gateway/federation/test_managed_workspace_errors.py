"""远端托管清单与删除入口的 HTTP 拒绝和业务阻塞合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes import workspaces_managed
from app.gateway.routes._shared import get_registry, get_workspace_runtime_controller
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager


@dataclass
class ManagementReply:
    status: int = 200
    payload: object = None
    content: bytes | None = None
    error: Exception | None = None
    requests: list[httpx.Request] = field(default_factory=list)


@pytest.fixture
def managed_list() -> dict[str, object]:
    return {"gateway_id": "gateway_remote", "gateway_name": "远端", "connection_kind": "local", "items": []}


@pytest.fixture
def blocked_removal() -> dict[str, object]:
    return {
        "gateway_id": "gateway_remote",
        "workspace_id": "remote-workspace", "backend_generation": "generation-original",
        "affected_workspace_ids": ["remote-workspace", "remote-sibling"], "status": "blocked",
        "blockers": [{"kind": "job", "gateway_id": "gateway_remote", "workspace_id": "remote-sibling", "resource_id": "job-original",
                      "session_id": "session-original", "status": "running", "detail": "原执行仍在运行"}],
        "workspaces": {"active_workspace": None, "items": []},
    }


@pytest.fixture
def management_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed_list: dict[str, object]):
    from app.gateway.main import gateway_http_exception_handler

    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="rgw_management", peer_gateway_id="gateway_local",
    )
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_management", name="指定远端", host="remote.test", port=22,
            username="local", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id="gateway_remote", protocol_version=1, source_owner="manual",
        ), runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}),
    )
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(gateway_http_exception_handler)
    application.include_router(workspaces_managed.router)
    application.dependency_overrides[verify_gateway_token] = lambda: "local-token"
    application.dependency_overrides[get_registry] = lambda: registry
    application.dependency_overrides[get_workspace_runtime_controller] = lambda: None
    forwards = AsyncMock()
    application.dependency_overrides[get_port_forward_manager] = lambda: forwards
    refresh = AsyncMock()
    monkeypatch.setattr(workspaces_managed, "refresh_remote_gateway_projections", refresh)
    reply = ManagementReply(payload={"data": managed_list})
    client_class = httpx.AsyncClient
    incoming = client_class(transport=httpx.ASGITransport(app=application), base_url="http://gateway")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-BoxTeam-Federation-Token"] == credential.token
        reply.requests.append(request)
        if reply.error is not None:
            raise reply.error
        if reply.content is not None:
            return httpx.Response(reply.status, content=reply.content)
        return httpx.Response(reply.status, json=reply.payload)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond), trust_env=False,
    ))
    yield incoming, reply, registry, refresh, forwards


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "remove"])
@pytest.mark.parametrize("status", [400, 404, 409, 503])
@pytest.mark.parametrize("location", ["/private/management-failure", r"C:\private\management-failure", r"\\host\share\management-failure"])
async def test_remote_http_rejection_keeps_status_without_copying_body_and_can_retry(
    management_route, managed_list: dict[str, object], operation: str, status: int, location: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, reply, registry, refresh, forwards = management_route
    reply.status, reply.payload = status, {"detail": location}
    method, path = ("GET", "/api/gateway/managed-workspaces") if operation == "list" else (
        "DELETE", "/api/gateway/managed-workspaces/remote-workspace",
    )
    before = registry.registry_revision
    async with client:
        response = await client.request(method, path, params={"gateway_connection_id": "rgw_management"},
                                        headers={"X-Request-ID": "req_management_failure"})
        assert response.status_code == (status if status < 500 else 502), response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req_management_failure"
        stage = "workspace_list" if operation == "list" else "workspace_remove"
        assert f"stage={stage}, error=HTTPStatusError" in response.json()["detail"]
        assert location not in response.text and "management-failure" not in response.text
        originals = [record.exc_info[1] for record in caplog.records if record.exc_info
                     and isinstance(record.exc_info[1], httpx.HTTPStatusError)]
        assert any(error.response.json() == {"detail": location} for error in originals)
        assert registry.registry_revision == before
        refresh.assert_not_awaited()
        forwards.reconcile_workspaces.assert_not_awaited()
        reply.status, reply.payload = 200, {"data": managed_list}
        recovered = await client.request(method, path, params={"gateway_connection_id": "rgw_management"},
                                         headers={"X-Request-ID": "req_management_retry"})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"] == {**managed_list, "gateway_connection_id": "rgw_management",
                                             "gateway_name": "指定远端", "connection_kind": "remote_gateway"}
        assert recovered.json()["request_id"] == recovered.headers["X-Request-ID"] == "req_management_retry"
        refresh.assert_awaited_once_with(registry=registry, connection_id="rgw_management")
        forwards.reconcile_workspaces.assert_awaited_once()
    assert [request.headers["X-Request-ID"] for request in reply.requests] == ["req_management_failure", "req_management_retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"detail": None}, {"detail": {"status": "blocked"}}, {"detail": {"status": "stopped"}}])
async def test_malformed_removal_conflict_stays_explicit_and_preserves_remote_status(management_route, payload: object) -> None:
    client, reply, _, refresh, forwards = management_route
    reply.status, reply.payload = 409, payload
    async with client:
        response = await client.delete("/api/gateway/managed-workspaces/remote-workspace",
                                       params={"gateway_connection_id": "rgw_management"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "工作区删除失败: stage=workspace_remove, error=HTTPStatusError"
    refresh.assert_not_awaited()
    forwards.reconcile_workspaces.assert_not_awaited()


@pytest.mark.asyncio
async def test_removal_conflict_preserves_complete_blocker_and_group_identity(
    management_route, blocked_removal: dict[str, object],
) -> None:
    client, reply, registry, refresh, forwards = management_route
    reply.status, reply.payload = 409, {"detail": blocked_removal}
    before = registry.registry_revision
    async with client:
        response = await client.delete("/api/gateway/managed-workspaces/remote-workspace",
                                       params={"gateway_connection_id": "rgw_management"},
                                       headers={"X-Request-ID": "req_removal_blocked"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == blocked_removal
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req_removal_blocked"
    assert registry.registry_revision == before
    refresh.assert_not_awaited()
    forwards.reconcile_workspaces.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "remove"])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.parametrize("error_type", [LookupError, PermissionError, RuntimeError, httpx.ConnectError])
async def test_remote_failure_keeps_original_cause_even_when_diagnostic_sink_fails(
    management_route, operation: str, sink_fails: bool, error_type: type[Exception],
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reply, registry, refresh, forwards = management_route
    original = error_type("连接失败: /private/management-failure")
    reply.error = original
    if sink_fails:
        def fail_log(*_, **__):
            raise RuntimeError("management diagnostic sink failed")
        monkeypatch.setattr("app.core.failures.logger.error", fail_log)
    before = registry.registry_revision
    method, path = ("GET", "/api/gateway/managed-workspaces") if operation == "list" else (
        "DELETE", "/api/gateway/managed-workspaces/remote-workspace",
    )
    async with client:
        response = await client.request(method, path, params={"gateway_connection_id": "rgw_management"},
                                        headers={"X-Request-ID": "req_management_native_failure"})
    status = 404 if error_type is LookupError else (409 if operation == "remove" and error_type is PermissionError else 502)
    assert response.status_code == status, response.text
    assert "management-failure" not in response.text
    assert f"error={error_type.__name__}" in response.json()["detail"]
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req_management_native_failure"
    if sink_fails:
        assert "diagnostic_sink_error=RuntimeError" in response.json()["detail"]
        assert any("management diagnostic sink failed" in note for note in original.__notes__)
    else:
        assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert registry.registry_revision == before
    refresh.assert_not_awaited()
    forwards.reconcile_workspaces.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_json_removal_conflict_is_rejected_without_changing_http_status(management_route) -> None:
    client, reply, _, refresh, forwards = management_route
    reply.status, reply.content = 409, b"<html>/private/management-failure</html>"
    async with client:
        response = await client.delete("/api/gateway/managed-workspaces/remote-workspace",
                                       params={"gateway_connection_id": "rgw_management"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "工作区删除失败: stage=workspace_remove, error=HTTPStatusError"
    refresh.assert_not_awaited()
    forwards.reconcile_workspaces.assert_not_awaited()
