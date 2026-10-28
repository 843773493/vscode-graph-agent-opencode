from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.control.resource_catalog import GatewayResourceCatalogService
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


@pytest.mark.asyncio
async def test_catalog_loads_unassociated_workspace_browser_without_source_session(target: WorkspaceTarget) -> None:
    calls: list[str] = []
    def request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        return httpx.Response(200, json={"data": {"workspace_id": WORKSPACE_ID, "items": [{
            "resource_id": "browser_owned", "workspace_id": WORKSPACE_ID, "kind": "browser",
            "retention_scope": "workspace", "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
            "thread_associations": [], "owner_revision": 3, "status": "running",
            "reconciliation_status": "verified", "unattached_since": None, "reclaim_at": None,
            "available_actions": ["stop", "delete"], "metadata": {"title": "持久浏览器"},
            "created_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z", "error": None,
        }]}})
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
