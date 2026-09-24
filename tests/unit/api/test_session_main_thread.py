"""GET /sessions/{session_id}/main-thread 权威 main pointer 合同测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_session_service
from app.api.sessions import resolve_session_main_thread, router
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture()
def workspace(tmp_path: Path):
    context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def service(workspace) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )


async def _create_session(service: SessionService) -> str:
    created = await service.create(SessionCreateRequest(title="普通会话"))
    return created.session_id


@pytest.mark.asyncio
async def test_main_thread_endpoint_returns_authoritative_thread(
    service: SessionService,
    workspace,
) -> None:
    session_id = await _create_session(service)
    # 独立读 catalog 冻结行，避免与被测方法同源而自证。
    frozen = workspace.store.get_node(session_id).main_thread_id

    response = await resolve_session_main_thread(
        session_id,
        _="local",
        request_id="req_main_thread",
        session_service=service,
    )

    assert response.request_id == "req_main_thread"
    assert response.data is not None
    assert response.data.session_id == session_id
    assert response.data.thread_id == frozen
    assert response.data.thread_id != session_id


@pytest.mark.asyncio
async def test_main_thread_endpoint_maps_missing_session_to_404(
    service: SessionService,
) -> None:
    with pytest.raises(HTTPException) as raised:
        await resolve_session_main_thread(
            "ses_00000000000040008000000000000000",
            _="local",
            request_id="req_missing",
            session_service=service,
        )

    assert raised.value.status_code == 404


def test_main_thread_endpoint_http_envelope(service: SessionService) -> None:
    session_id = asyncio.run(_create_session(service))
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as client:
            unauthorized = client.get(
                f"/api/v1/sessions/{session_id}/main-thread"
            )
            assert unauthorized.status_code == 401
            authorized = client.get(
                f"/api/v1/sessions/{session_id}/main-thread",
                headers={"X-Local-Token": "local-dev-token"},
            )
    finally:
        app.dependency_overrides.clear()

    assert authorized.status_code == 200
    assert authorized.headers["X-Request-ID"]
    body = authorized.json()
    assert body["code"] == 0
    assert body["request_id"] == authorized.headers["X-Request-ID"]
    assert body["data"]["session_id"] == session_id
    assert body["data"]["thread_id"].startswith("thr_")
    assert body["data"]["thread_id"] != session_id
