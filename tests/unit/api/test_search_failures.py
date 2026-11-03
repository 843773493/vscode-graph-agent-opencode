"""共享搜索 HTTP 边界保留领域错误，通用失败不能误标为文件 owner。"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import search
from app.api.deps import get_workspace_search_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.services.business.search.service import WorkspaceSearchService
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)


@pytest.fixture
def search_api():
    query = AsyncMock()
    service = Mock(spec=WorkspaceSearchService, search=query)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(search.router, prefix="/api/v1")
    application.dependency_overrides[verify_local_token] = lambda: "test-search-token"
    application.dependency_overrides[get_workspace_search_service] = lambda: service
    with TestClient(application, raise_server_exceptions=False,
                    headers={"X-BoxTeam-Gateway-Id": "gateway_" + "a" * 32, "X-Request-ID": "req-search-failure"}) as client:
        yield client, query


@pytest.mark.parametrize("category,error,status,code", [
    ("catalog", TimeoutError("本次总期限已到"), 409, "deadline"),
    ("file", TimeoutError("本次总期限已到"), 409, "deadline"),
    ("file", ResourceFileOperationError("invalid_encoding", "不是 UTF-8 正文", reason="utf8"), 400, "invalid_encoding"),
    ("llm_request", PermissionError(13, "original request log failure"), 500, None),
])
def test_domain_failures_keep_original_http_ownership(search_api, category, error, status, code):
    client, query = search_api
    query.side_effect = error
    response = client.post("/api/v1/search", json={"workspace_id": "ws", "category": category, "query": "needle"})
    assert response.status_code == status
    query.assert_awaited_once()
    if code is not None:
        assert response.json()["detail"]["code"] == code
        assert response.headers["X-Request-ID"] == "req-search-failure"
