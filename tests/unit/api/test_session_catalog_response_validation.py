"""目录内部响应损坏不能冒充输入拒绝或分页冲突。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError

from app.api import session_navigation
from app.api.deps import (
    get_session_catalog_service,
    get_workspace_search_service,
    verify_local_token,
)
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session_navigation import SessionCatalogPageDTO
from app.services.business.search.providers import register_navigation_search
from app.services.business.search.service import WorkspaceSearchService
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_service import SessionService

BASE = "/api/v1/session-catalog"
REQUEST_ID = "req-catalog-validation"
NATIVE_PATHS = ["/private/catalog-secret", r"C:\x\catalog-secret", r"\\target\x\catalog-secret"]
OPERATION_ID = "op_" + "a" * 32
ENQUEUE = {
    "intents": [{
        "client_operation_id": OPERATION_ID,
        "client_sequence": 1,
        "kind": "create_folder",
        "base_catalog_revision": 0,
        "name": "校验目录",
    }],
}


@dataclass
class CatalogCase:
    client: TestClient
    service: Mock
    application: FastAPI


def _search_service(catalog: SessionCatalogReadService) -> WorkspaceSearchService:
    service = WorkspaceSearchService("workspace-validation")
    register_navigation_search(service, catalog, Mock(spec=SessionService))
    return service


@pytest.fixture
def catalog_case() -> Iterator[CatalogCase]:
    from app.main import app as workspace_app

    service = Mock(spec=SessionCatalogService, workspace_id="workspace-validation")
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(session_navigation.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(
            error_type, workspace_app.exception_handlers[error_type]
        )
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_session_catalog_service] = lambda: service
    search_service = _search_service(service)
    application.dependency_overrides[get_workspace_search_service] = lambda: search_service
    with TestClient(application, raise_server_exceptions=False, headers={
        "X-Request-ID": REQUEST_ID,
        "X-BoxTeam-Gateway-Id": "gateway_789f146c159aa74461af8dacca7341ca",
        "X-BoxTeam-Navigation-Actor": "user:test-user",
    }) as client:
        yield CatalogCase(client, service, application)


def _request(client: TestClient, route: str) -> Response:
    if route == "operations:enqueue":
        return client.post(BASE + "/" + route, json=ENQUEUE)
    path, _, cursor = route.partition("?cursor=")
    params = {"query": "校验", "operation_id": OPERATION_ID}
    if cursor:
        params["cursor"] = cursor
    return client.get(BASE + "/" + path, params=params)


@pytest.mark.parametrize(("route", "method"), [
    ("operations:enqueue", "submit_operation_batch"),
    ("operations", "operation_status"),
    ("navigation-events", "navigation_events"),
    ("navigation-events?cursor=fixture", "decode_navigation_events_cursor"),
    ("children", "list_children"),
    ("roots", "list_children"),
    ("search", "search"),
])
@pytest.mark.parametrize("native", NATIVE_PATHS, ids=["posix", "drive", "unc"])
def test_internal_validation_keeps_original_error_in_diagnostics(
    catalog_case: CatalogCase,
    route: str,
    method: str,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ValidationError) as caught:
        SessionCatalogPageDTO.model_validate({
            "revision": {native: native}, "catalog_revision": 1,
        })
    failure = caught.value
    getattr(catalog_case.service, method).side_effect = failure
    response = _request(catalog_case.client, route)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert "catalog-secret" not in response.text
    diagnostics = [r for r in caplog.records if r.exc_info and r.exc_info[1] is failure]
    assert len(diagnostics) == 1 and "catalog-secret" in caplog.text
    if method == "decode_navigation_events_cursor":
        catalog_case.service.navigation_events.assert_not_called()


@pytest.mark.parametrize(("route", "method", "status"), [
    ("operations:enqueue", "submit_operation_batch", 400),
    ("operations", "operation_status", 400),
    ("navigation-events", "navigation_events", 409),
    ("navigation-events?cursor=fixture", "decode_navigation_events_cursor", 409),
    ("children", "list_children", 409),
    ("roots", "list_children", 409),
    ("search", "search", 409),
])
def test_business_rejection_preserves_status_and_request_id(
    catalog_case: CatalogCase, route: str, method: str, status: int,
) -> None:
    getattr(catalog_case.service, method).side_effect = ValueError("目录输入或游标无效")
    response = _request(catalog_case.client, route)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": "目录输入或游标无效", "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID
    if method == "decode_navigation_events_cursor":
        catalog_case.service.navigation_events.assert_not_called()


@pytest.mark.parametrize("route", ["children", "roots", "search"])
@pytest.mark.parametrize("native", NATIVE_PATHS, ids=["posix", "drive", "unc"])
def test_real_read_service_response_retries_after_source_metadata_repair(
    catalog_case: CatalogCase, route: str, native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # resolver 只提供局部查询元数据；快照、分页与响应装配使用真实目录读链路。
    resolver = Mock(spec=SessionCatalogPathResolver, revision=1)
    resolver.catalog_snapshot.return_value = ({native: native}, [])
    sessions = Mock(spec=SessionService, path_resolver=resolver)
    service = SessionCatalogReadService(session_service=sessions)
    catalog_case.application.dependency_overrides[get_session_catalog_service] = lambda: service
    search_service = _search_service(service)
    catalog_case.application.dependency_overrides[get_workspace_search_service] = lambda: search_service
    response = _request(catalog_case.client, route)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert "catalog-secret" not in response.text and "catalog-secret" in caplog.text
    diagnostics = [r for r in caplog.records if r.exc_info and isinstance(r.exc_info[1], ValidationError)]
    assert len(diagnostics) == 1
    # 修复同一来源，下一次读取须重建正确快照；不靠清缓存或重造服务掩盖问题。
    resolver.catalog_snapshot.return_value = (1, [])
    repaired = _request(catalog_case.client, route)
    assert repaired.status_code == 200, repaired.text
    expected: dict[str, object] = {
        "revision": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "catalog_revision": 1,
        "items": [], "cursor": None, "total": 0,
    }
    if route != "search":
        expected.update(parent_node_id=None, consistency_warning=None)
    assert repaired.json() == {"code": 0, "message": "ok", "data": expected, "request_id": REQUEST_ID}
    assert repaired.headers["X-Request-ID"] == REQUEST_ID
    assert resolver.catalog_snapshot.call_count == 2
