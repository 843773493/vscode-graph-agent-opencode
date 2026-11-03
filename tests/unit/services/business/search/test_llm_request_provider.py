"""真实 catalog 创建限定请求日志来源；所有读取仍交给原日志 owner。"""

import json

import pytest

from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.search.providers import register_llm_request_search
from app.services.business.search.service import WorkspaceSearchService
from app.services.business.session_navigation import SessionCatalogService
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from tests.unit.services.business.session_navigation.test_fuzzy_catalog_search import (
    catalog as catalog,  # noqa: PLC0414
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def provider(catalog: SessionCatalogService):
    sessions = catalog._session_service
    logs = LLMRequestLogService(catalog._path_resolver.sessions_root, path_resolver=sessions.path_resolver)
    service = WorkspaceSearchService(catalog.workspace_id)
    register_llm_request_search(service, catalog, sessions, logs)
    return service, logs


async def test_provider_pages_original_session_records_and_binds_owner(catalog, provider):
    service, logs = provider
    sessions = catalog._session_service
    owners = [await sessions.create(SessionCreateRequest(title=f"请求来源 {index}")) for index in range(2)]
    for owner in owners:
        directory = sessions.path_resolver.resolve_session_node_for_runtime(owner.session_id) / "logs" / "llm_requests"
        directory.mkdir(parents=True)
        (directory / "1000.json").write_text(json.dumps({"session_id": owner.session_id, "timestamp": 100,
            "request": {"prompt": "请求词😀"}, "response": {}}), encoding="utf-8")
    request = WorkspaceSearchRequest(workspace_id=catalog.workspace_id, category="llm_request", operation="content",
                                     match_mode="literal", query="请求词😀", limit=1)
    hits = []
    cursor = None
    for _ in range(5):
        result = await service.search(request.model_copy(update={"cursor": cursor}), gateway_id=GATEWAY)
        assert result.llm_requests and result.domain_page is result.llm_requests
        hits.extend(result.llm_requests.items)
        cursor = result.llm_requests.cursor
        if result.state == "complete":
            break
    assert result.state == "complete"
    assert {hit.match.reference.session_id for hit in hits} == {owner.session_id for owner in owners}
    for hit in hits:
        assert hit.session_uri.endswith(f"/resources/session/{hit.match.reference.session_id}")
        assert logs.locate(hit.match.reference).text[hit.match.reference.start_codepoint:hit.match.reference.end_codepoint] == "请求词😀"
    first = await service.search(request, gateway_id=GATEWAY)
    assert first.llm_requests.cursor
    with pytest.raises(ValueError, match="归属、查询或目录版本不符"):
        await service.search(request.model_copy(update={"cursor": first.llm_requests.cursor}), gateway_id="another-gateway")
    await sessions.create(SessionCreateRequest(title="目录变动"))
    with pytest.raises(ValueError, match="目录版本不符"):
        await service.search(request.model_copy(update={"cursor": first.llm_requests.cursor}), gateway_id=GATEWAY)


@pytest.fixture
def request_log_http(catalog, provider):
    from fastapi import FastAPI

    from app.api.deps import (
        get_llm_request_log_service,
        get_session_service,
        get_workspace_search_service,
        verify_local_token,
    )
    from app.api.search import router as search_router
    from app.api.sessions import router as sessions_router
    from app.core.trace_middleware import TraceMiddleware

    service, logs = provider
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.dependency_overrides[get_llm_request_log_service] = lambda: logs
    app.dependency_overrides[get_session_service] = lambda: catalog._session_service
    app.dependency_overrides[get_workspace_search_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    return app


async def test_http_search_locate_rejects_deleted_and_other_session(catalog, request_log_http):
    import httpx

    sessions = catalog._session_service
    owner = await sessions.create(SessionCreateRequest(title="原请求"))
    other = await sessions.create(SessionCreateRequest(title="其他 Session"))
    directory = sessions.path_resolver.resolve_session_node_for_runtime(owner.session_id) / "logs" / "llm_requests"
    directory.mkdir(parents=True)
    path = directory / "1000.json"
    path.write_text(json.dumps({"session_id": owner.session_id, "timestamp": 100,
                               "request": {"prompt": "位置😀"}, "response": {}}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request_log_http), base_url="http://test",
        headers={"X-Request-ID": "log-search-http", "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
        response = await client.post("/api/v1/search", json={"workspace_id": catalog.workspace_id,
            "category": "llm_request", "operation": "content", "match_mode": "literal", "query": "位置😀"})
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "log-search-http"
        page = response.json()["data"]["llm_requests"]
        assert page["complete"] and len(page["items"]) == 1
        reference = page["items"][0]["match"]["reference"]
        endpoint = f"/api/v1/sessions/{owner.session_id}/llm-request-logs/locate"
        located = await client.post(endpoint, json=reference)
        assert located.status_code == 200, located.text
        data = located.json()["data"]
        assert data["text"][reference["start_codepoint"]:reference["end_codepoint"]] == "位置😀"
        assert data["record"]["file_name"] == "1000.json"
        mismatch = await client.post(f"/api/v1/sessions/{other.session_id}/llm-request-logs/locate", json=reference)
        assert mismatch.status_code == 400 and mismatch.json()["detail"]["code"] == "owner_mismatch"
        path.unlink()
        stale = await client.post(endpoint, json=reference)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_request_log_reference"
