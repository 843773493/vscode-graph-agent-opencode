from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from tests.support.resource_owners.sessions import (
    SESSION_A,
    THREAD_A,
    WORKSPACE_ID,
)


@pytest.fixture
def target() -> WorkspaceTarget:
    return WorkspaceTarget(workspace_id="gw_test", name="测试工作区", root_path="/unused/catalog",
                           backend_url="http://workspace.test", connection_kind="local",
                           backend_workspace_id=WORKSPACE_ID, managed=True)


@pytest.fixture
def resource_record() -> dict[str, object]:
    return {
        "resource_id": "browser_owned", "workspace_id": WORKSPACE_ID, "kind": "browser",
        "retention_scope": "workspace", "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
        "thread_associations": [], "owner_revision": 3, "status": "running",
        "reconciliation_status": "verified", "unattached_since": None, "reclaim_at": None,
        "available_actions": ["stop", "delete"], "metadata": {"title": "持久浏览器"},
        "created_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z", "error": None,
    }


@pytest.mark.asyncio
async def test_catalog_loads_unassociated_workspace_browser_without_source_session(
    target: WorkspaceTarget, resource_record: dict[str, object],
) -> None:
    calls: list[str] = []
    def request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": [resource_record]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service=GatewayResourceCatalogService(registry=Mock(spec=GatewayWorkspaceRegistry), http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_resources")
    assert errors == []
    assert len(resources) == 1
    assert resources[0].workspace_id == target.workspace_id == "gw_test"
    assert resources[0].resource.workspace_id == WORKSPACE_ID
    assert resources[0].resource.thread_associations == []
    assert resources[0].resource.retention_scope == "workspace"
    assert calls == ["http://workspace.test/api/v1/resources"]


@pytest.mark.asyncio
async def test_catalog_owner_failure_exposes_workspace_error(target: WorkspaceTarget) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        service=GatewayResourceCatalogService(registry=Mock(spec=GatewayWorkspaceRegistry), http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_failed")
    assert resources == []
    assert len(errors) == 1
    assert errors[0].scope_key == "gw_test"
    assert "503" in errors[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize("native_path", [
    "/private/catalog-marker/state.sqlite", r"C:\private\catalog-marker\state.sqlite",
    r"\\server\private\catalog-marker\state.sqlite",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("failure", ["envelope", "validation", "transport", "http-status"])
async def test_catalog_failure_preserves_scope_and_diagnostics_without_raw_payload(
    target: WorkspaceTarget, native_path: str, failure: str, caplog: pytest.LogCaptureFixture,
) -> None:
    healthy = replace(target, workspace_id="gw_healthy", name="健康工作区", backend_url="http://healthy.test")
    registry = Mock(spec=GatewayWorkspaceRegistry)
    registry.targets.return_value = (target, healthy)
    calls: list[httpx.Request] = []
    transport_error = httpx.ReadError(f"目录传输失败: {native_path}")
    failed = True

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-Request-ID"] == "req_catalog_failure"
        assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        if request.url.host == "workspace.test" and failed:
            if failure == "envelope":
                return httpx.Response(200, json={"message": f"目录读取失败: {native_path}"})
            if failure == "validation":
                return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": native_path}})
            if failure == "transport":
                raise transport_error
            return httpx.Response(503, extensions={"reason_phrase": native_path.encode()})
        return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service = GatewayResourceCatalogService(registry=registry, http_client=client)
        result = await service.list(request_id="req_catalog_failure")
        assert result.items == []
        error, = result.errors
        assert error.scope_key == target.workspace_id
        assert error.label == "本机 Gateway · 测试工作区"
        assert "catalog-marker" not in result.model_dump_json()
        expected_type = {"envelope": "ValueError", "validation": "ValidationError", "transport": "ReadError", "http-status": "HTTPStatusError"}[failure]
        assert expected_type in error.message
        if failure == "http-status":
            assert "503" in error.message
        diagnostic, = [record for record in caplog.records if record.name == "app.gateway.control.resource_catalog"]
        assert diagnostic.exc_info is not None
        original = diagnostic.exc_info[1]
        assert original is not None
        if isinstance(original, ValidationError):
            assert original.errors()[0]["input"] == native_path
        else:
            assert native_path in str(original)
        assert target.workspace_id in diagnostic.getMessage()
        assert "req_catalog_failure" in diagnostic.getMessage()
        if failure == "transport":
            assert original is transport_error
        if isinstance(original, httpx.HTTPStatusError):
            assert original.response.status_code == 503
            assert original.response.extensions["reason_phrase"] == native_path.encode()
        assert {call.url.host for call in calls} == {"workspace.test", "healthy.test"}
        assert len(calls) == 2

        failed = False
        recovered = await service.list(request_id="req_catalog_failure")
        assert recovered.items == [] and recovered.errors == []
        assert len(calls) == 4


@pytest.fixture(params=["local", "remote_gateway"])
def owner_target(
    request: pytest.FixtureRequest, target: WorkspaceTarget, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[WorkspaceTarget, Mock, str | None]:
    registry = Mock(spec=GatewayWorkspaceRegistry)
    token = None
    if request.param == "remote_gateway":
        target = replace(target, connection_kind="remote_gateway", root_path=None, managed=False,
                         remote_gateway_connection_id="rgw_owner", remote_workspace_id="route_on_peer")
        registry.remote_gateway_connection.return_value.name = "远端 Gateway"
        registry.remote_gateway_url.return_value = "http://peer.test"
        monkeypatch.setattr("app.gateway.control.resource_catalog.get_gateway_root", lambda: tmp_path)
        credential = FederationCredentialStore(storage_path=tmp_path / "credentials/federation.json").issue(
            connection_id="rgw_owner", peer_gateway_id="gateway_peer",
        )
        token = credential.token
    registry.targets.return_value = (target,)
    return target, registry, token


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["envelope-empty", "envelope-with-data", "item-after-valid"])
@pytest.mark.parametrize("foreign_owner", [
    "00000000-0000-4000-8000-000000000099", "/private/catalog-owner-marker/state.sqlite",
    r"C:\private\catalog-owner-marker\state.sqlite", r"\\server\catalog-owner-marker\state.sqlite",
], ids=["other-workspace", "posix", "drive", "unc"])
async def test_catalog_rejects_foreign_owner_before_publishing_any_target_items(
    owner_target: tuple[WorkspaceTarget, Mock, str | None], resource_record: dict[str, object],
    position: str, foreign_owner: str, caplog: pytest.LogCaptureFixture,
) -> None:
    target, registry, token = owner_target
    original_target = asdict(target)
    failed = True
    calls = []

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-Request-ID"] == "req_catalog_owner"
        assert request.headers["X-BoxTeam-Workspace-Id"] == ("route_on_peer" if token else WORKSPACE_ID)
        if token:
            assert request.headers["X-BoxTeam-Federation-Token"] == token
        if not failed:
            data = {"workspace_id": WORKSPACE_ID, "items": [resource_record]}
        elif position == "item-after-valid":
            data = {"workspace_id": WORKSPACE_ID, "items": [resource_record, {**resource_record, "resource_id": "browser_foreign", "workspace_id": foreign_owner}]}
        else:
            data = {"workspace_id": foreign_owner, "items": [] if position == "envelope-empty" else [resource_record]}
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service = GatewayResourceCatalogService(registry=registry, http_client=client)
        rejected = await service.list(request_id="req_catalog_owner")
        assert rejected.items == []
        error, = rejected.errors
        assert error.scope_key == target.workspace_id
        assert error.message == "工作区资源目录读取失败: ValueError"
        assert "catalog-owner-marker" not in rejected.model_dump_json()
        diagnostic, = [record for record in caplog.records if record.name == "app.gateway.control.resource_catalog"]
        assert diagnostic.exc_info is not None
        assert isinstance(diagnostic.exc_info[1], ValueError)
        assert foreign_owner in str(diagnostic.exc_info[1])
        assert WORKSPACE_ID in str(diagnostic.exc_info[1])
        assert len(calls) == 1 and asdict(target) == original_target

        failed = False
        recovered = await service.list(request_id="req_catalog_owner")
        item, = recovered.items
        assert recovered.errors == [] and len(calls) == 2
        assert item.workspace_id == target.workspace_id
        assert item.resource.model_dump() == resource_record
        assert asdict(target) == original_target
