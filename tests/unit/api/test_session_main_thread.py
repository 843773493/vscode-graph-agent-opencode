"""GET /sessions/{session_id}/main-thread 权威 main pointer 合同测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from shutil import rmtree

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_session_service, verify_local_token
from app.api.sessions import resolve_session_main_thread, router
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


@pytest.fixture()
def catalog_workspace(
    request: pytest.FixtureRequest,
    empty_workspace_scope: tuple[str, Path],
) -> Iterator[CatalogWorkspaceContext]:
    workspace_id = empty_workspace_scope[0]
    run_context = TestRunContext.from_test_file(Path(request.node.path))
    context = build_catalog_workspace(run_context.output_root, workspace_id=workspace_id)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def session_service(catalog_workspace: CatalogWorkspaceContext) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=catalog_workspace.sessions_root),
        workspace_id=catalog_workspace.workspace_id,
        path_resolver=catalog_workspace.resolver,
        creation_service=catalog_workspace.creation_service,
    )


async def _create_session(session_service: SessionService) -> str:
    created = await session_service.create(SessionCreateRequest(title="普通会话"))
    return created.session_id


def _build_session_api_app(
    session_service: SessionService,
    *,
    authenticate_local_requests: bool,
    map_workspace_http_errors: bool,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    if map_workspace_http_errors:
        app.add_exception_handler(HTTPException, workspace_http_exception_handler)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: session_service
    if authenticate_local_requests:
        app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    return app


@pytest.mark.parametrize("physical_exists", [True, False])
def test_session_detail_rejects_deleting_owner_before_physical_read(
    session_service: SessionService,
    catalog_workspace: CatalogWorkspaceContext,
    physical_exists: bool,
) -> None:
    session_id = asyncio.run(_create_session(session_service))
    catalog_workspace.store.set_node_state(session_id, "deleting")
    if not physical_exists:
        rmtree(catalog_workspace.session_dir(session_id))
    app = _build_session_api_app(
        session_service,
        authenticate_local_requests=True,
        map_workspace_http_errors=True,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("session_deletion_pending:")
    assert str(catalog_workspace.workspace_root) not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


@pytest.mark.asyncio
async def test_main_thread_endpoint_returns_authoritative_thread(
    session_service: SessionService,
    catalog_workspace: CatalogWorkspaceContext,
) -> None:
    session_id = await _create_session(session_service)
    # 独立读 catalog 冻结行，避免与被测方法同源而自证。
    frozen = catalog_workspace.store.get_node(session_id).main_thread_id

    response = await resolve_session_main_thread(
        session_id,
        _="local",
        request_id="req_main_thread",
        session_service=session_service,
    )

    assert response.request_id == "req_main_thread"
    assert response.data is not None
    assert response.data.session_id == session_id
    assert response.data.thread_id == frozen
    assert response.data.thread_id != session_id


@pytest.mark.asyncio
async def test_main_thread_endpoint_maps_missing_session_to_404(
    session_service: SessionService,
) -> None:
    with pytest.raises(HTTPException) as raised:
        await resolve_session_main_thread(
            "ses_00000000000070008000000000000000",
            _="local",
            request_id="req_missing",
            session_service=session_service,
        )

    assert raised.value.status_code == 404


def test_main_thread_endpoint_http_envelope(session_service: SessionService) -> None:
    session_id = asyncio.run(_create_session(session_service))
    app = _build_session_api_app(
        session_service,
        authenticate_local_requests=False,
        map_workspace_http_errors=False,
    )
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
    assert not (
        session_service.path_resolver.resolve_session_node(session_id) / "rollout"
    ).exists()
