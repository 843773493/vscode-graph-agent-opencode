"""AGENTS 发现只描述固定来源；正文与错误继续沿原导航 owner。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import resources
from app.api.deps import (
    get_resource_navigation,
    get_workspace_service,
    verify_local_token,
)
from app.core.resource_uri import workspace_agent_spec_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@pytest.fixture
def agent_spec_api(empty_workspace_scope: tuple[str, Path]) -> Iterator[tuple[TestClient, Path, str]]:
    from app.main import app as workspace_app

    workspace_id, root = empty_workspace_scope
    owner = ResourceNavigation(workspace_root=root, workspace_id=workspace_id,
        file_paths=WorkspaceFilePaths(workspace=root, workspace_id=workspace_id, bindings=None))
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(resources.router, prefix="/api/v1")
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    application.dependency_overrides[verify_local_token] = lambda: "agent-spec-test-token"
    application.dependency_overrides[get_resource_navigation] = lambda: owner
    application.dependency_overrides[get_workspace_service] = lambda: SimpleNamespace(workspace_id=workspace_id)
    with TestClient(application, headers={"X-BoxTeam-Gateway-Id": "gateway_" + "a" * 32,
                    "X-Request-ID": "agent-spec-discovery"}) as client:
        yield client, root, workspace_agent_spec_display_uri(workspace_id)


def test_discovery_returns_original_typed_metadata_without_body_or_storage_write(
    agent_spec_api: tuple[TestClient, Path, str],
) -> None:
    client, root, uri = agent_spec_api
    content = "# 原工作区 AGENTS\n不得激活此来源 😀\n"
    (root / "AGENTS.md").write_text(content, encoding="utf-8")
    before = set(root.rglob("*"))
    response = client.get("/api/v1/resources/agent-spec")
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "agent-spec-discovery"
    data = response.json()["data"]
    assert data["node"]["uri"] == uri and data["node"]["resource_shape"] == "file"
    assert data["node"]["operations"] == ["describe", "read"]
    assert data["node"]["stat"]["size"] == len(content.encode())
    assert data["content"] is None and data["entries"] == [] and data["matches"] == []
    assert set(root.rglob("*")) == before
    original = client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "describe"})
    assert original.json() == response.json()


@pytest.mark.parametrize("shape", ["missing", "directory"])
def test_unavailable_source_keeps_original_navigation_error(
    agent_spec_api: tuple[TestClient, Path, str], shape: str,
) -> None:
    client, root, uri = agent_spec_api
    if shape == "directory":
        (root / "AGENTS.md").mkdir()
    discovered = client.get("/api/v1/resources/agent-spec")
    original = client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "describe"})
    assert discovered.status_code == original.status_code != 200
    assert discovered.json() == original.json()
    assert discovered.headers["X-Request-ID"] == discovered.json()["request_id"] == "agent-spec-discovery"
