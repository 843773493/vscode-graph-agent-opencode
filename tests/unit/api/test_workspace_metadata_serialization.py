"""通过实际 FastAPI 响应序列化验证两个元数据入口的失败边界。"""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_workspace_service, verify_local_token
from app.api.workspace import router
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.workspace import WorkspaceContextDTO, WorkspaceDTO


@pytest.fixture
def metadata_owner() -> SimpleNamespace:
    return SimpleNamespace(
        get=AsyncMock(return_value=WorkspaceDTO(workspace_id="workspace-unit", name="项目")),
        get_context=AsyncMock(return_value=WorkspaceContextDTO(workspace_id="workspace-unit")),
    )


@pytest.fixture
def metadata_client(metadata_owner: SimpleNamespace) -> Iterator[TestClient]:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_workspace_service] = lambda: metadata_owner
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("model,required", [("WorkspaceDTO", ["workspace_id", "name"]), ("WorkspaceContextDTO", ["workspace_id"])])
def test_metadata_response_openapi_preserves_declared_fields(metadata_client: TestClient, model: str, required: list[str]) -> None:
    response = metadata_client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()["components"]["schemas"][model]
    assert schema["required"] == required
    assert {"workspace_id", "project_type", "git"} <= schema["properties"].keys()
    assert "root_path" not in schema["properties"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("endpoint", ["/api/v1/workspace", "/api/v1/workspace/context"])
def test_metadata_api_serializes_identity_and_preserves_request_id(metadata_client: TestClient, endpoint: str) -> None:
    response = metadata_client.get(endpoint, headers={"X-Request-ID": "workspace-metadata-request"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"] == "workspace-metadata-request"
    assert payload["data"]["workspace_id"] == "workspace-unit"
    assert "root_path" not in payload["data"] and "root" not in payload["data"]["git"]


@pytest.mark.parametrize("endpoint,method", [("/api/v1/workspace", "get"), ("/api/v1/workspace/context", "get_context")])
def test_metadata_api_preserves_paths_in_user_text_and_commands(metadata_client: TestClient, metadata_owner: SimpleNamespace, endpoint: str, method: str) -> None:
    expected_git_metadata = {"content": "/tmp/example", "command": ["/usr/bin/python", r"C:\Users\owner\input"],
        "changed_paths": ["/agent/provider"]}
    getattr(metadata_owner, method).return_value.git.update(expected_git_metadata)
    response = metadata_client.get(endpoint)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["git"] == expected_git_metadata


@pytest.mark.parametrize("endpoint,method", [("/api/v1/workspace", "get"), ("/api/v1/workspace/context", "get_context")])
@pytest.mark.parametrize("path", ["/private/workspace", r"C:\Users\owner\workspace", r"\\host\share\workspace"])
def test_metadata_api_fails_before_serializing_nested_real_path(metadata_client: TestClient, metadata_owner: SimpleNamespace, endpoint: str, method: str, path: str) -> None:
    getattr(metadata_owner, method).return_value.git["nested"] = {"locations": [path]}
    response = metadata_client.get(endpoint)
    assert response.status_code == 500
    assert path not in response.text
    assert "workspace-unit" not in response.text
