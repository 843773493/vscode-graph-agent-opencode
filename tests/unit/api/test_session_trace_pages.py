"""Session Trace HTTP 分页的大历史边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_session_service, verify_local_token
from app.api.sessions import router
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobStartedEvent,
    JobStartedPayload,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.session_trace.trace_database import TraceDatabase
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.api_helpers import running_job_dispatch_snapshot
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


@pytest.fixture()
def catalog_workspace(
    empty_workspace_scope: tuple[str, Path],
) -> Iterator[CatalogWorkspaceContext]:
    workspace_id = empty_workspace_scope[0]
    context = build_catalog_workspace(
        empty_workspace_scope[1].parent,
        workspace_id=workspace_id,
    )
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def trace_session_service(catalog_workspace: CatalogWorkspaceContext) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=catalog_workspace.sessions_root),
        workspace_id=catalog_workspace.workspace_id,
        path_resolver=catalog_workspace.resolver,
        creation_service=catalog_workspace.creation_service,
    )


def _build_app(session_service: SessionService) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, workspace_http_exception_handler)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: session_service
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    return app


def test_session_trace_http_pages_hundred_thousand_events_without_unbounded_response(
    trace_session_service: SessionService,
) -> None:
    session_id = asyncio.run(
        trace_session_service.create(SessionCreateRequest(title="大型 Trace"))
    ).session_id
    main_thread_id = trace_session_service.path_resolver.main_thread_id(session_id)
    job_id = "job_large_trace"
    store = trace_session_service._trace_event_store
    timestamp = datetime.now(UTC)

    async def append_owner() -> None:
        await store.append(
            session_id,
            JobStartedEvent(
                event_id="evt_large_owner",
                job_id=job_id,
                timestamp=timestamp,
                payload=JobStartedPayload(
                    session_id=session_id,
                    thread_id=main_thread_id,
                    dispatch=running_job_dispatch_snapshot(
                        session_id,
                        main_thread_id,
                        job_id,
                        queued_job_count=0,
                        queue_snapshot_version=0,
                    ),
                ),
            ),
        )

    asyncio.run(append_owner())
    template = AgentStartEvent(
        event_id="evt_large_00000",
        job_id=job_id,
        agent_id="default",
        timestamp=timestamp,
        payload=AgentStartPayload(message="fixture", agent_id="default"),
    ).model_dump_json() + "\n"

    def records():
        offset = 0
        for index in range(99_999):
            raw = template.replace(
                "evt_large_00000",
                f"evt_large_{index:05d}",
            )
            yield offset, raw
            offset += len(raw.encode("utf-8"))

    database_path = store._database_path(session_id)
    with TraceDatabase(database_path).connection(write=True) as connection:
        assert connection is not None
        tail = connection.execute(
            "SELECT end FROM trace_events ORDER BY start DESC LIMIT 1"
        ).fetchone()
        offset = tail[0]
        connection.executemany(
            "INSERT INTO trace_events(start, raw) VALUES (?, ?)",
            ((offset + start, raw) for start, raw in records()),
        )

    app = _build_app(trace_session_service)
    try:
        with TestClient(app) as client:
            first = client.get(
                f"/api/v1/sessions/{session_id}/traces",
                params={"limit": 3},
            )
            assert first.status_code == 200, first.text
            first_data = first.json()["data"]
            assert [item["event_id"] for item in first_data["items"]] == [
                "evt_large_99996",
                "evt_large_99997",
                "evt_large_99998",
            ]
            assert first_data["has_more"] is True
            cursor = first_data["next_cursor"]
            assert isinstance(cursor, str) and cursor
            assert "evt_large_99996" not in cursor

            second = client.get(
                f"/api/v1/sessions/{session_id}/traces",
                params={"limit": 3, "cursor": cursor},
            )
            assert second.status_code == 200, second.text
            second_data = second.json()["data"]
            assert [item["event_id"] for item in second_data["items"]] == [
                "evt_large_99993",
                "evt_large_99994",
                "evt_large_99995",
            ]
            assert second_data["has_more"] is True
            assert second_data["next_cursor"] != cursor
            for response, data in ((first, first_data), (second, second_data)):
                assert len(data["items"]) == 3
                assert all(
                    item["session_id"] == session_id
                    and item["thread_id"] == main_thread_id
                    for item in data["items"]
                )
                assert len(response.content) < 16 * 1024
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
    finally:
        app.dependency_overrides.clear()
