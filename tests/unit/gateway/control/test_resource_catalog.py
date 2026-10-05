from __future__ import annotations

import httpx
import pytest

from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import WorkspaceTarget
from tests.support.terminal_owner import SESSION_A, THREAD_A, WORKSPACE_ID


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
        service=GatewayResourceCatalogService(registry=object(), http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_resources")
    assert errors == []
    assert len(resources) == 1
    assert resources[0].resource.thread_associations == []
    assert resources[0].resource.retention_scope == "workspace"
    assert calls == ["http://workspace.test/api/v1/resources"]


@pytest.mark.asyncio
async def test_catalog_owner_failure_exposes_workspace_error(target: WorkspaceTarget) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        service=GatewayResourceCatalogService(registry=object(), http_client=client)
        resources, errors=await service._load_workspace(target, request_id="req_failed")
    assert resources == []
    assert len(errors) == 1
    assert errors[0].scope_key == "gw_test"
    assert "503" in errors[0].message
