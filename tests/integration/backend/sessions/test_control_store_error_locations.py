"""控制库损坏经真实 Session API 拒绝，响应不泄漏物理位置。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.types import ExceptionHandler

from app import main
from app.api.deps import (
    get_session_service,
    get_session_skill_tracking_service,
    verify_local_token,
)
from app.api.sessions import router
from app.container import AppContainer
from app.core.session_control_store import SessionControlStore
from app.core.trace_middleware import TraceMiddleware
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)


@pytest.fixture
async def control_client(startup_container: AppContainer):
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(HTTPException, cast(ExceptionHandler, main.workspace_http_exception_handler))
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_session_service] = lambda: startup_container.session_service
    application.dependency_overrides[get_session_skill_tracking_service] = lambda: startup_container.session_skill_tracking_service
    application.dependency_overrides[verify_local_token] = lambda: "control-location-test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


async def test_damaged_control_version_returns_safe_conflict_without_mutation(
    startup_container: AppContainer, control_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = await control_client.post("/api/v1/sessions", json={"title": "控制库故障恢复"})
    assert created.status_code == 200, created.text
    session = created.json()["data"]
    session_id = session["session_id"]
    main_thread = await control_client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert main_thread.status_code == 200, main_thread.text
    thread_id = main_thread.json()["data"]["thread_id"]
    database = startup_container.session_service.path_resolver.resolve_session_node(session_id) / "session-control.sqlite"
    with closing(sqlite3.connect(database)) as corruption:
        corruption.execute("PRAGMA user_version = 6")
    before = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as inspection:
        rows_before = "\n".join(inspection.iterdump())
    url = f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack"
    response = await control_client.post(url, json={"name": "example"},
                                         headers={"X-Request-ID": "control-version-conflict"})
    assert response.status_code == 409, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "control-version-conflict"
    assert "artifact=session_control" in response.text and "stage=schema_version" in response.text
    assert "user_version=6, supported=18" in response.text
    assert str(startup_container.workspace_root) not in response.text
    assert str(database) in caplog.text
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as repair:
        assert "\n".join(repair.iterdump()) == rows_before
        repair.execute(f"PRAGMA user_version = {SessionControlStore.SCHEMA_VERSION}")
    retry = await control_client.post(url, json={"name": "example"},
                                      headers={"X-Request-ID": "control-version-retry"})
    # 修复后到达真实 CSM：不存在的 Skill 返回领域错误，不能伪造 untrack 成功。
    assert retry.status_code == 200, retry.text
    result = retry.json()["data"]
    assert result["session_id"] == session_id and result["thread_id"] == thread_id
    assert result["status"] == "error" and result["error"]["code"] == "skill-not-found"
    assert result["append_status"] == "none" and not result["tracked"] and not result["queued"]
    assert retry.headers["X-Request-ID"] == retry.json()["request_id"] == "control-version-retry"
    assert str(startup_container.workspace_root) not in retry.text
