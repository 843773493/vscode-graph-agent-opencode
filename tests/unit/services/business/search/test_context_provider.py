"""原上下文 owner 与统一 Provider 的来源、身份和分页一致。"""

import base64
import json
import time

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.abstractions.session_context import (
    SessionContextCursorError,
    SessionContextRevisionChangedError,
)
from app.api.context import router as context_router
from app.api.deps import (
    get_session_context_query_service,
    get_workspace_search_service,
    verify_local_token,
)
from app.api.search import router as search_router
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.search import (
    WorkspaceContextSearchOptionsDTO,
    WorkspaceSearchRequest,
)
from app.schemas.internal_v2.session_context import SessionContextSearchRequest
from app.schemas.search_federation import FederatedSearchQueryDTO
from app.services.business.search.context import register_context_search
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.unit.services.business.test_session_context_query_service import (
    BACKEND_WORKSPACE_ID,
    _FakeMessageSource,
    _session_request,
)
from tests.unit.services.business.test_session_context_query_service import (
    session_context_service_factory as session_context_service_factory,  # noqa: PLC0414 - 复用原来源装具，不准备另一套历史。
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def context_source(session_context_service_factory, monkeypatch):
    monkeypatch.setattr("app.services.business.session_context_query_service.get_current_gateway_id", lambda: GATEWAY)
    return session_context_service_factory(_FakeMessageSource())


@pytest.fixture
def search_service(context_source):
    service = WorkspaceSearchService(BACKEND_WORKSPACE_ID)
    register_context_search(service, context_source)
    return service


@pytest.fixture
def context_http(context_source, search_service):
    from app.main import app as workspace_app

    app = FastAPI()
    app.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    app.add_middleware(TraceMiddleware)
    app.include_router(context_router, prefix="/api/v1")
    app.include_router(search_router, prefix="/api/v1")
    app.dependency_overrides[get_session_context_query_service] = lambda: context_source
    app.dependency_overrides[get_workspace_search_service] = lambda: search_service
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    return app


@pytest.mark.parametrize(("source", "query"), [
    ("effective_context", "问题"), ("session_catalog", "目标会话"), ("session_information", "current_tool"),
])
async def test_original_sources_and_owner_page_are_preserved(context_source, search_service, source, query):
    request = WorkspaceSearchRequest.model_validate({
        "workspace_id": BACKEND_WORKSPACE_ID, "category": "context", "operation": "content", "match_mode": "literal",
        "query": query, "limit": 1, "context": {"sources": [source], "case_sensitive": True},
    })
    page = await search_service.search(request, gateway_id=GATEWAY)
    direct = await context_source.search_context(SessionContextSearchRequest(
        resource_identity=workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
        uri=workspace_session_inventory_uri(BACKEND_WORKSPACE_ID, gateway_authority=GATEWAY),
        scope="workspace", query=query, sources=[source], case_sensitive=True, max_results=1,
    ))
    assert page.context == direct and page.context.matches
    assert page.result_count == 1 and page.cursor == direct.next_cursor
    assert all(hit.source == source and hit.uri.startswith(f"boxteam://{GATEWAY}/") for hit in direct.matches)
    if direct.has_more:
        following = await search_service.search(request.model_copy(update={"cursor": page.cursor}), gateway_id=GATEWAY)
        assert following.context is not None and following.context.matches != direct.matches
        changed = request.model_copy(update={"cursor": page.cursor, "query": "另一个来源"})
        with pytest.raises(ValueError, match="cursor"):
            await search_service.search(changed, gateway_id=GATEWAY)


async def test_default_is_effective_context_and_options_are_real(search_service):
    base = {"workspace_id": BACKEND_WORKSPACE_ID, "category": "context", "operation": "content", "query": "alpha", "match_mode": "literal"}
    page = await search_service.search(WorkspaceSearchRequest.model_validate(base), gateway_id=GATEWAY)
    assert page.context is not None and len(page.context.matches) == 2
    assert {hit.source for hit in page.context.matches} == {"effective_context"}
    strict = await search_service.search(WorkspaceSearchRequest.model_validate({**base, "context": {"case_sensitive": True}}), gateway_id=GATEWAY)
    assert strict.result_count == 0 and strict.state == "complete"
    regex = await search_service.search(WorkspaceSearchRequest.model_validate({**base, "query": "ALP.A", "match_mode": "regex"}), gateway_id=GATEWAY)
    assert regex.context is not None and regex.context.matches == page.context.matches
    capability, = search_service.capabilities().providers
    assert capability.category == "context" and capability.match_modes == ("literal", "regex")


async def test_revision_character_budget_and_deadline_remain_explicit(search_service):
    request = WorkspaceSearchRequest(workspace_id=BACKEND_WORKSPACE_ID, category="context", operation="content", query="问题", match_mode="literal")
    full = await search_service.search(request, gateway_id=GATEWAY)
    assert full.context is not None and full.state == "complete"
    budget_chars = full.context.returned_chars - 1
    clipped = await search_service.search(request.model_copy(update={"context": WorkspaceContextSearchOptionsDTO(max_chars=budget_chars)}), gateway_id=GATEWAY)
    assert clipped.context is not None and clipped.context.returned_chars <= budget_chars
    assert clipped.state == "limited" and clipped.context.matches
    with pytest.raises(SessionContextRevisionChangedError, match="revision"):
        await search_service.search(request.model_copy(update={"context": WorkspaceContextSearchOptionsDTO(revision="stale")}), gateway_id=GATEWAY)
    with pytest.raises(TimeoutError):
        await search_service.search(request.model_copy(update={"deadline_at": time.time() - 1}), gateway_id=GATEWAY)
    with pytest.raises(SearchContractError, match="Gateway"):
        await search_service.search(request)


@pytest.mark.parametrize("model", [WorkspaceSearchRequest, FederatedSearchQueryDTO])
def test_context_options_cannot_change_target_or_apply_to_other_category(model):
    fields = {"workspace_id": BACKEND_WORKSPACE_ID} if model is WorkspaceSearchRequest else {}
    with pytest.raises(ValidationError, match="只有上下文"):
        model.model_validate({**fields, "category": "catalog", "query": "query", "context": {}})
    with pytest.raises(ValidationError):
        model.model_validate({**fields, "category": "context", "query": "query", "context": {"uri": "another-owner"}})
    with pytest.raises(ValidationError):
        model.model_validate({**fields, "category": "context", "query": "query", "context": {"sources": []}})


async def test_old_and_unified_http_entries_keep_same_revision_conflict(context_http):
    import httpx

    owner = {"resource_identity": workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
             "uri": workspace_session_inventory_uri(BACKEND_WORKSPACE_ID, gateway_authority=GATEWAY), "scope": "workspace"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context_http), base_url="http://test",
        headers={"X-Request-ID": "req_context_stale", "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
        responses = [
            await client.post("/api/v1/context/search", json={**owner, "query": "问题", "revision": "stale"}),
            await client.post("/api/v1/context/read", json={**owner, "view": "inventory", "revision": "stale"}),
            await client.post("/api/v1/search", json={"workspace_id": BACKEND_WORKSPACE_ID, "category": "context", "operation": "content",
                "match_mode": "literal", "query": "问题", "context": {"revision": "stale"}}),
        ]
    for response in responses:
        assert response.status_code == 409, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "req_context_stale"
        detail = response.json()["detail"]
        assert detail["code"] == "snapshot_changed" and detail["expected_revision"] == "stale"
        assert detail["actual_revision"] and detail["actual_revision"] != "stale"


@pytest.mark.parametrize("mutation,reason", [
    ("invalid_base64", "malformed"), ("array", "malformed"),
    ("resource_identity", "owner_mismatch"), ("uri", "owner_mismatch"), ("scope", "owner_mismatch"),
    ("operation", "query_mismatch"), ("record_index", "query_mismatch"), ("assembly_ref", "query_mismatch"),
    ("negative", "position_invalid"), ("boolean", "position_invalid"), ("fraction", "position_invalid"),
    ("char_boolean", "position_invalid"), ("past_end", "position_out_of_bounds"),
    ("char_past_end", "position_out_of_bounds"), ("end_with_char", "position_out_of_bounds"),
])
@pytest.mark.parametrize("entry", ["read", "search", "unified"])
async def test_cursor_rejection_keeps_original_owner_across_http_entries(context_http, entry, mutation, reason):
    import httpx

    owner = {"resource_identity": workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
             "uri": workspace_session_inventory_uri(BACKEND_WORKSPACE_ID, gateway_authority=GATEWAY), "scope": "workspace"}
    if entry == "read":
        endpoint = "/api/v1/context/read"
        request = _session_request(view="messages", limit=1).model_dump(mode="json")
    elif entry == "search":
        endpoint, request = "/api/v1/context/search", {**owner, "query": "问题", "max_results": 1}
    else:
        endpoint, request = "/api/v1/search", {"workspace_id": BACKEND_WORKSPACE_ID, "category": "context",
                                              "operation": "content", "match_mode": "literal", "query": "问题", "limit": 1}
    request_id = f"req_cursor_{entry}_{mutation}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context_http), base_url="http://test",
        headers={"X-Request-ID": request_id, "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
        first = await client.post(endpoint, json=request)
        assert first.status_code == 200, first.text
        page = first.json()["data"]
        if entry == "unified":
            page = page["context"]
        cursor = page["next_cursor"]
        assert cursor is not None
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if mutation == "invalid_base64":
            cursor = "!!"
        elif mutation == "array":
            cursor = base64.urlsafe_b64encode(b"[]").decode().rstrip("=")
        else:
            if mutation in {"resource_identity", "uri", "scope", "operation", "record_index", "assembly_ref"}:
                payload[mutation] = "other"
            elif mutation == "negative":
                payload["offset"] = -1
            elif mutation == "boolean":
                payload["offset"] = True
            elif mutation == "fraction":
                payload["offset"] = 1.5
            elif mutation == "char_boolean":
                payload["char_offset"] = True
            elif mutation == "past_end":
                payload["offset"] = 999999
            elif mutation == "char_past_end":
                payload["char_offset"] = 999999
                if entry != "read":
                    reason = "position_invalid"
            else:
                if entry == "read":
                    full = await client.post(endpoint, json={**request, "limit": 200})
                    assert full.status_code == 200 and not full.json()["data"]["has_more"], full.text
                    total = len(full.json()["data"]["items"])
                else:
                    total = page["total_matches"]
                payload.update(offset=total, char_offset=1)
                if entry != "read":
                    reason = "position_invalid"
            cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        rejected = await client.post(endpoint, json={**request, "cursor": cursor})
    assert rejected.status_code == 400, rejected.text
    assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == request_id
    detail = rejected.json()["detail"]
    assert detail["owner"] == SessionContextCursorError.owner and detail["code"] == SessionContextCursorError.code
    assert detail["reason"] == reason and detail["message"]
