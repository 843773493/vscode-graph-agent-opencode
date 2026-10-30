"""产品创建的 Session 在 catalog 隔离后仍有明确且不带位置的错误响应。"""

from __future__ import annotations

import logging
import sqlite3

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import main
from app.api import sessions
from app.api.deps import get_session_service
from app.container import AppContainer
from app.core.trace_middleware import TraceMiddleware
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


@pytest.mark.parametrize("fault", ["closed", "rollback"])
async def test_catalog_isolation_response_and_cold_reopen_preserve_original_session(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture, fault: str,
) -> None:
    container = startup_container

    async def handle_http_error(http_request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, HTTPException)
        return await main.workspace_http_exception_handler(http_request, error)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, handle_http_error)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: container.session_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://catalog.test",
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        created = await client.post("/api/v1/sessions", json={"title": "catalog隔离原件"})
        assert created.status_code == 200, created.text
        session = created.json()["data"]
        session_id = session["session_id"]
        healthy = await client.get("/api/v1/sessions")
        assert healthy.status_code == 200 and healthy.json()["data"]["items"] == [session]
        store = container.session_service.path_resolver.catalog_store
        native = sqlite3.OperationalError(f"回滚失败，数据库已隔离: {store.database_path}")
        if fault == "rollback":
            connection = store.connection

            class RollbackFailure:
                @property
                def in_transaction(self) -> bool:
                    return connection.in_transaction

                def execute(self, statement: str) -> sqlite3.Cursor:
                    if statement == "ROLLBACK":
                        raise native
                    return connection.execute(statement)

                def close(self) -> None:
                    connection.close()

            primary = RuntimeError("事务业务失败")
            with monkeypatch.context() as faults:
                faults.setattr(store, "_connection", RollbackFailure())
                with pytest.raises(RuntimeError) as caught, store.read_transaction():
                    raise primary
            assert caught.value is primary and store._rollback_failure is native and store._closed
            assert any("回滚失败" in note for note in primary.__notes__)
            assert any(str(store.database_path) in note for note in native.__notes__)
        path, files = _close_and_files(container)
        before = path.read_bytes()
        caplog.set_level(logging.ERROR)
        for suffix in ("", f"/{session_id}"):
            response = await client.get("/api/v1/sessions" + suffix, headers={"X-Request-ID": "catalog-isolated"})
            assert response.status_code == 409, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "catalog-isolated"
            assert str(path) not in response.text and str(container.workspace_root) not in response.text
            assert "已关闭" in response.text and "artifact=session_catalog" in response.text
            assert "stage=catalog-" in response.text
        assert str(path) in caplog.text
        if fault == "rollback":
            assert any(record.exc_info and record.exc_info[1] is native for record in caplog.records)
        assert path.read_bytes() == before
        _assert_body_unchanged(container, files)

        reopened = _build_owned_container(container.workspace_root, monkeypatch, request)
        app.dependency_overrides[get_session_service] = lambda: reopened.session_service
        recovered = await client.get("/api/v1/sessions")
        assert recovered.status_code == 200 and recovered.json()["data"]["items"] == [session]
        detail = await client.get(f"/api/v1/sessions/{session_id}")
        assert detail.status_code == 200 and detail.json()["data"] == session
        assert path.read_bytes() == before
        _assert_body_unchanged(container, files)
