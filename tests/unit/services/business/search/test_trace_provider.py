"""真实 Session 入口限定来源，HTTP 保留原 Trace 错误与精确定位身份。"""

import asyncio
import base64
import json
import threading
from datetime import UTC, datetime

import httpx
import pytest

from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
)
from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.search.providers import register_trace_search
from app.services.business.search.service import WorkspaceSearchService
from tests.unit.services.business.session_navigation.test_fuzzy_catalog_search import (
    catalog as catalog,  # noqa: PLC0414
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def provider(catalog):
    sessions = catalog._session_service
    traces = sessions._trace_event_store
    service = WorkspaceSearchService(catalog.workspace_id)
    register_trace_search(service, catalog, sessions, traces)
    return service, traces


@pytest.fixture
async def records(catalog, provider):
    _service, traces = provider
    owners = [await catalog._session_service.create(SessionCreateRequest(title=f"轨迹来源 {index}")) for index in range(2)]
    for owner in owners:
        main = catalog._path_resolver.main_thread_id(owner.session_id)
        await traces.append(owner.session_id, JobCreatedEvent(event_id=f"create-{owner.session_id}",
            job_id=f"job-{owner.session_id}", timestamp=datetime.now(UTC), payload=JobCreatedPayload(
                session_id=owner.session_id, thread_id=main, message="来源", agent_id="default")))
        await traces.append(owner.session_id, AgentStartEvent(event_id=f"start-{owner.session_id}",
            job_id=f"job-{owner.session_id}", timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="目标😀 trace😀 trace😀", agent_id="default")))
    return owners


@pytest.fixture
def trace_http(catalog, provider):
    from fastapi import FastAPI, HTTPException

    from app.api.deps import (
        get_session_service,
        get_trace_event_store,
        get_workspace_search_service,
        verify_local_token,
    )
    from app.api.search import router as search_router
    from app.api.sessions import router as sessions_router
    from app.core.trace_middleware import TraceMiddleware
    from app.main import workspace_http_exception_handler

    service, traces = provider
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.exception_handler(HTTPException)(workspace_http_exception_handler)
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.dependency_overrides[get_trace_event_store] = lambda: traces
    app.dependency_overrides[get_session_service] = lambda: catalog._session_service
    app.dependency_overrides[get_workspace_search_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    return app


async def test_provider_continues_across_sessions_and_locates_exact_event(catalog, provider, records):
    service, traces = provider
    request = WorkspaceSearchRequest(workspace_id=catalog.workspace_id, category="trace", operation="content",
        match_mode="literal", query="trace😀", limit=1)
    cursor = None
    hits = []
    for _ in range(8):
        result = await service.search(request.model_copy(update={"cursor": cursor}), gateway_id=GATEWAY)
        assert result.traces is result.domain_page and result.result_count == len(result.traces.items)
        hits.extend(result.traces.items)
        cursor = result.cursor
        if result.state == "complete":
            break
        assert cursor is not None
    assert result.state == "complete" and len(hits) == 4
    assert {hit.match.reference.session_id for hit in hits} == {owner.session_id for owner in records}
    for hit in hits:
        reference = hit.match.reference
        assert hit.session_uri.endswith(f"/resources/session/{reference.session_id}")
        assert reference.thread_id == catalog._path_resolver.main_thread_id(reference.session_id)
        located = traces.locate(reference)
        assert located.text[reference.start_codepoint:reference.end_codepoint] == "trace😀"


async def test_http_locate_owner_staleness_and_request_id(catalog, trace_http, records):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=trace_http), base_url="http://test",
        headers={"X-Request-ID": "trace-read-http", "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
        response = await client.post("/api/v1/search", json={"workspace_id": catalog.workspace_id,
            "category": "trace", "operation": "content", "match_mode": "literal", "query": "目标😀"})
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "trace-read-http"
        hits = response.json()["data"]["traces"]["items"]
        assert len(hits) == 2
        reference = hits[0]["match"]["reference"]
        endpoint = f"/api/v1/sessions/{reference['session_id']}/traces/locate"
        located = await client.post(endpoint, json=reference)
        assert located.status_code == 200, located.text
        data = located.json()["data"]
        assert data["record"]["event_id"] == reference["event_id"]
        assert data["text"][reference["start_codepoint"]:reference["end_codepoint"]] == "目标😀"
        other = next(owner for owner in records if owner.session_id != reference["session_id"])
        rejected = await client.post(f"/api/v1/sessions/{other.session_id}/traces/locate", json=reference)
        assert rejected.status_code == 400 and rejected.json()["detail"]["code"] == "owner_mismatch"
        rejected = await client.post(endpoint, json=reference | {"event_id": "wrong-event"})
        assert rejected.status_code == 409 and rejected.json()["detail"]["reason"] == "owner_mismatch"


@pytest.mark.parametrize("change,code,reason", [
    ("malformed", "invalid_trace_search_cursor", "malformed"),
    ("owner", "invalid_trace_search_cursor", "owner_mismatch"),
    ("query", "invalid_trace_search_cursor", "query_mismatch"),
    ("position", "invalid_trace_search_cursor", "position_out_of_bounds"),
    ("catalog", "stale_trace_search_cursor", "catalog_changed"),
    ("file", "stale_trace_search_cursor", "file_changed"),
])
async def test_http_cursor_keeps_machine_reason(catalog, trace_http, records, change, code, reason):
    request = {"workspace_id": catalog.workspace_id, "category": "trace", "operation": "content",
        "match_mode": "literal", "query": "trace😀", "limit": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=trace_http), base_url="http://test",
        headers={"X-Request-ID": "trace-cursor-http", "X-BoxTeam-Gateway-Id": GATEWAY}) as client:
        first = await client.post("/api/v1/search", json=request)
        assert first.status_code == 200, first.text
        cursor = first.json()["data"]["traces"]["cursor"]
        assert cursor is not None
        state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if change == "malformed":
            cursor = "%%%"
        elif change in {"owner", "query", "position"}:
            if change == "position":
                state["position"]["before_offset"] = 10000000
            else:
                state["workspace_id" if change == "owner" else "query"] = "different"
            cursor = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
        elif change == "catalog":
            await catalog._session_service.create(SessionCreateRequest(title="目录变动"))
        else:
            for owner in records:
                path = catalog._path_resolver.resolve_session_node_for_runtime(owner.session_id) / "logs/traces/events.jsonl"
                with path.open("ab") as stream:
                    stream.write(b"\n")
        rejected = await client.post("/api/v1/search", json=request | {"cursor": cursor})
        assert rejected.status_code == (400 if code == "invalid_trace_search_cursor" else 409), rejected.text
        assert rejected.json()["detail"] == {"owner": "trace_event_store", "code": code,
            "reason": reason, "message": rejected.json()["detail"]["message"]}
        assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == "trace-cursor-http"


async def test_cancel_waits_for_original_worker_before_settlement(catalog, provider, records, monkeypatch):
    service, traces = provider
    started, release = threading.Event(), threading.Event()
    original = traces.search_session_events
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(5), "测试必须最终释放 worker"
        return original(*args, **kwargs)
    monkeypatch.setattr(traces, "search_session_events", blocked)
    task = asyncio.create_task(service.search(WorkspaceSearchRequest(workspace_id=catalog.workspace_id,
        category="trace", operation="content", match_mode="literal", query="目标😀"), gateway_id=GATEWAY))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
