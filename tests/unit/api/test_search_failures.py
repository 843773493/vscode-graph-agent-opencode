"""共享搜索 HTTP 边界保留领域错误，通用失败不能误标为文件 owner。"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import search, session_navigation, session_turns
from app.api.deps import get_workspace_search_service, verify_local_token
from app.core.session_control_thread_catalog.search import (
    ThreadCatalogRevisionChangedError,
)
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.search import SearchProviderCapabilityDTO
from app.services.business.search.service import WorkspaceSearchService
from app.services.business.session_navigation.thread_search import (
    ThreadSearchCursorError,
)
from app.services.business.session_turn_history.workspace_search import (
    WorkspaceMessageSearchCursorError,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)


@pytest.fixture
def search_api():
    from app.main import app as workspace_app

    query = AsyncMock()
    service = Mock(spec=WorkspaceSearchService, search=query, workspace_id="ws")
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    application.include_router(search.router, prefix="/api/v1")
    application.include_router(session_navigation.router, prefix="/api/v1")
    application.include_router(session_turns.router, prefix="/api/v1")
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


@pytest.mark.parametrize("error", [
    ThreadSearchCursorError("malformed", "Thread 搜索 cursor 格式无效"),
    ThreadSearchCursorError("catalog_revision", "Thread 搜索目录已更新"),
    ThreadCatalogRevisionChangedError("session-exact", 1, 2),
])
@pytest.mark.parametrize("endpoint", ["/api/v1/search", "/api/v1/session-threads/search"])
def test_thread_cursor_rejections_keep_named_owner_on_both_routes(search_api, endpoint, error):
    client, query = search_api
    query.side_effect = error
    response = (client.post(endpoint, json={"workspace_id": "ws", "category": "thread", "query": "needle"})
                if endpoint == "/api/v1/search" else client.get(endpoint, params={"query": "needle"}))
    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req-search-failure"
    assert response.json()["detail"] == error.detail()
    query.assert_awaited_once()


@pytest.mark.parametrize("error,status", [
    (WorkspaceMessageSearchCursorError("malformed", "原消息续点格式无效"), 400),
    (WorkspaceMessageSearchCursorError("catalog_revision", "原消息目录已更新"), 409),
    (ThreadCatalogRevisionChangedError("session-exact", 1, 2), 409),
])
@pytest.mark.parametrize("endpoint", ["/api/v1/search", "/api/v1/sessions/history/search"])
def test_workspace_message_cursor_keeps_same_detail_on_both_routes(search_api, endpoint, error, status):
    client, query = search_api
    query.side_effect = error
    payload = {"query": "needle", "cursor": "original-cursor"}
    if endpoint == "/api/v1/search":
        payload.update(workspace_id="ws", category="message", operation="content", match_mode="literal")
    response = client.post(endpoint, json=payload)
    assert response.status_code == status
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "req-search-failure"
    assert response.json()["detail"] == error.detail()
    query.assert_awaited_once()


@pytest.fixture
def provider_search_api(search_api):
    client, query = search_api
    service = WorkspaceSearchService("ws")
    service.register(SearchProviderCapabilityDTO(
        provider_id="llm-request-log", category="llm_request", operations=("content",), contract_version=1,
        supported_options=("query",), position_kind="request_log_codepoint", max_results=100,
        cache_policy="none", match_modes=("literal",),
    ), query)
    client.app.dependency_overrides[get_workspace_search_service] = lambda: service
    return client, query


@pytest.mark.parametrize("error,status,code", [
    (PermissionError(13, "Permission denied", "/target/project/request.json"), 500, "provider_read_failed"),
    (TimeoutError("期限已到"), 409, "deadline"),
])
def test_registered_provider_failure_is_serialized_with_real_owner(provider_search_api, error, status, code):
    client, query = provider_search_api
    query.side_effect = error
    response = client.post("/api/v1/search", json={
        "workspace_id": "ws", "category": "llm_request", "operation": "content", "match_mode": "literal", "query": "needle",
    })
    assert response.status_code == status and response.headers["X-Request-ID"] == "req-search-failure"
    detail = response.json()["detail"]
    assert detail["code"] == code
    if code == "provider_read_failed":
        assert detail["owner"] == "search_provider" and detail["provider_id"] == "llm-request-log"
        assert detail["category"] == "llm_request" and detail["operation"] == "content"
        assert "/target/project/request.json" in detail["message"]
    query.assert_awaited_once()
