"""真实文件导航结果构造后损坏引用，HTTP发布必须拒绝且可沿原请求恢复。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI

from app.api import resources
from app.api.deps import get_resource_navigation, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

GATEWAY = "gateway_" + "a" * 32
REQUEST_ID = "req-navigation-publication"
NATIVE = ["/private/navigation-output-sentinel", r"C:\private\navigation-output-sentinel",
          r"\\target\private\navigation-output-sentinel", "file:///private/navigation-output-sentinel"]


@dataclass
class NavigationCase:
    owner: ResourceNavigation
    client: httpx.AsyncClient
    uri: str
    file: Path
    run: TestRunContext


@pytest.fixture
async def navigation_case(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> AsyncIterator[NavigationCase]:
    with TestRunContext.from_test_file(Path(request.node.path)) as run:
        root = prepare_default_test_workspace(
            workspace_root=Path(integration_workspace_root_path),
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        workspace_id = load_or_create_workspace_id(root)
        file = root / "navigation-publication.txt"
        file.write_text("needle /private/user-file-body\n", encoding="utf-8")
        paths = WorkspaceFilePaths(workspace=root, workspace_id=workspace_id, bindings=None)
        owner = ResourceNavigation(workspace_root=root, workspace_id=workspace_id, file_paths=paths)
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(resources.router, prefix="/api/v1")
        app.dependency_overrides[get_resource_navigation] = lambda: owner
        app.dependency_overrides[verify_local_token] = lambda: "test-local"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://navigation.test",
            headers={"X-Request-ID": REQUEST_ID, "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
            yield NavigationCase(owner, client, paths.workspace_uri + "/navigation-publication.txt", file, run)


@pytest.mark.parametrize("field", ["node", "entries", "matches"])
@pytest.mark.parametrize("native", NATIVE)
async def test_navigation_rejects_copied_native_reference_then_retries_original_request(
    navigation_case: NavigationCase, monkeypatch: pytest.MonkeyPatch,
    field: Literal["node", "entries", "matches"], native: str,
) -> None:
    case = navigation_case
    original = case.owner._response
    observed: list[ResourceNavigationResultDTO] = []

    def corrupted(
        result: ResourceNavigationResultDTO, request: ResourceNavigationRequest, deadline: float,
    ) -> ResourceNavigationResultDTO:
        result = original(result, request, deadline)
        assert result.node.uri == case.uri
        assert len(result.entries) == len(result.matches) == 1
        assert result.entries[0].uri == result.matches[0].uri == case.uri
        observed.append(result)
        if field == "node":
            return result.model_copy(update={"node": result.node.model_copy(update={"uri": native})})
        items = getattr(result, field)
        return result.model_copy(update={field: (items[0].model_copy(update={"uri": native}),)})

    payload = {"uri": case.uri, "operation": "search", "search_mode": "content", "query": "needle"}
    before = case.file.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(case.owner, "_response", corrupted)
        rejected = await case.client.post("/api/v1/resources/navigation", json=payload)
    assert rejected.status_code == 500, rejected.text
    assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == REQUEST_ID
    assert rejected.json()["data"] is None and "PydanticSerializationError" in rejected.json()["message"]
    assert "navigation-output-sentinel" not in rejected.text
    assert len(observed) == 1 and case.file.read_bytes() == before
    recovered = await case.client.post("/api/v1/resources/navigation", json=payload)
    assert recovered.status_code == 200, recovered.text
    assert recovered.headers["X-Request-ID"] == recovered.json()["request_id"] == REQUEST_ID
    result = recovered.json()["data"]
    assert result["node"]["uri"] == result["entries"][0]["uri"] == result["matches"][0]["uri"] == case.uri
    assert result["matches"][0]["text"] == "needle /private/user-file-body"
    assert case.file.read_bytes() == before
    assert observed[0].model_dump(mode="json") == result
    case.run.write_result("passed", {"field": field, "rejected": rejected.json(), "recovered": recovered.json(),
        "scope": "真实文件owner与ASGI发布；只损坏构造后的引用，不代Gateway/真实网络、模型执行或进程重启。"})
