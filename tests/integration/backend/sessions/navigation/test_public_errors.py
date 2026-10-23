"""产品创建的导航记录损坏时，普通响应须拒绝并保留完整诊断。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app import main
from app.api import session_navigation
from app.api.deps import get_session_catalog_service
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.trace_middleware import TraceMiddleware
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    _catalog_dump,
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

_GATEWAY = "gateway_789f146c159aa74461af8dacca7341ca"
_PRIVATE = "/private/native-navigation-artifact"


@pytest.fixture
async def navigation_api(
    startup_container: AppContainer,
) -> AsyncIterator[tuple[AppContainer, httpx.AsyncClient, str]]:
    container = startup_container
    catalog = container.session_catalog_service
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, main.workspace_http_exception_handler)
    app.include_router(session_navigation.router, prefix="/api/v1")
    app.dependency_overrides[get_session_catalog_service] = lambda: catalog
    await catalog.operations.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://navigation.test",
            headers={
                "X-Local-Token": "local-dev-token",
                "X-BoxTeam-Gateway-Id": _GATEWAY,
                "X-BoxTeam-Navigation-Actor": "user:test-user",
            },
        ) as client:
            operation_id = create_prefixed_id("op")
            response = await client.post("/api/v1/session-catalog/operations:enqueue", json={
                "intents": [{
                    "client_operation_id": operation_id, "client_sequence": 1,
                    "kind": "create_folder", "name": "错误响应原件",
                    "base_catalog_revision": catalog.path_resolver.revision,
                }],
            })
            assert response.status_code == 202, response.text
            for _ in range(200):
                status = await client.get("/api/v1/session-catalog/operations", params={"operation_id": operation_id})
                assert status.status_code == 200, status.text
                if status.json()["data"]["items"][0]["state"] == "committed":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("实际导航 worker 未提交创建目录")
            yield container, client, operation_id
    finally:
        await catalog.operations.stop()


@pytest.mark.parametrize("table,column,value,status", [
    ("navigation_events", "created_at", _PRIVATE, 500),
    ("navigation_events", "affected_node_ids_json", json.dumps({_PRIVATE: 1}), 500),
    ("navigation_mutation_records", "updated_at", _PRIVATE, 400),
    ("navigation_mutation_records", "kind", _PRIVATE, 400),
    ("navigation_mutation_records", "params_json", "extra-field", 400),
    ("navigation_mutation_records", "depends_on_json", _PRIVATE, 400),
    ("navigation_events", "operation_id", _PRIVATE, 500),
], ids=["event-time", "event-shape", "operation-time", "operation-kind", "operation-extra", "operation-json", "event-id"])
async def test_corrupt_record_api_rejects_without_native_locator(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    caplog: pytest.LogCaptureFixture, table: str, column: str, value: str, status: int,
) -> None:
    container, client, operation_id = navigation_api
    path = container.session_catalog_service.path_resolver.catalog_store.database_path
    with closing(sqlite3.connect(path)) as connection:
        if value == "extra-field":
            params = json.loads(connection.execute(
                "SELECT params_json FROM navigation_mutation_records WHERE operation_id=?", (operation_id,),
            ).fetchone()[0])
            value = json.dumps({**params, _PRIVATE: 1})
        connection.execute(f"UPDATE {table} SET {column}=? WHERE operation_id=?", (value, operation_id))
        connection.commit()
    before = _catalog_dump(path)
    caplog.set_level(logging.ERROR)
    is_event = table == "navigation_events"
    response = await client.get(
        "/api/v1/session-catalog/navigation-events" if is_event else "/api/v1/session-catalog/operations",
        params={} if is_event else {"operation_id": operation_id},
        headers={"X-Request-ID": "navigation-corruption-request"},
    )
    assert response.status_code == status, response.text
    assert response.headers["X-Request-ID"] == "navigation-corruption-request"
    assert response.json()["request_id"] == "navigation-corruption-request"
    for private in (_PRIVATE, str(path), str(container.workspace_root)):
        assert private not in response.text
    assert "navigation" in response.text and ("无效" in response.text or "损坏" in response.text)
    assert _PRIVATE in caplog.text, "完整原生诊断必须留在服务端日志"
    assert _catalog_dump(path) == before, "读取损坏历史不得改写或修复原记录"


async def test_worker_failure_api_keeps_503_without_copying_cause(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    container, client, operation_id = navigation_api
    operations = container.session_catalog_service.operations
    await operations.stop()

    async def fail_worker() -> None:
        raise CatalogMaintenanceRequiredError(f"原始存储诊断: {_PRIVATE}")

    # 故障注入只在 worker 边界；记录与事件由上面的真实产品入口生成。
    caplog.set_level(logging.ERROR)
    task = asyncio.create_task(fail_worker())
    await asyncio.sleep(0)
    operations._worker_task = task
    operations._observe_worker_exit(task)
    response = await client.get(
        "/api/v1/session-catalog/operations", params={"operation_id": operation_id},
        headers={"X-Request-ID": "navigation-worker-request"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["request_id"] == "navigation-worker-request"
    assert "CatalogMaintenanceRequiredError" in response.text
    assert "queue worker" in response.text and _PRIVATE not in response.text
    assert _PRIVATE in caplog.text


@pytest.mark.parametrize("table,column", [
    ("navigation_mutation_records", "kind"),
    ("navigation_events", "created_at"),
    ("navigation_queue_owners", "owner_generation"),
])
async def test_cold_startup_keeps_safe_error_and_original_diagnostics(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture, table: str, column: str,
) -> None:
    container, _, _ = navigation_api
    await container.session_catalog_service.operations.stop()
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (_PRIVATE,))
        connection.commit()
    before, raw = _catalog_dump(path), path.read_bytes()
    caplog.set_level(logging.ERROR)
    with pytest.raises(CatalogMaintenanceRequiredError) as failure:
        _build_owned_container(container.workspace_root, monkeypatch, request)
    for private in (_PRIVATE, str(path), str(container.workspace_root)):
        assert private not in str(failure.value)
    assert "原始记录损坏" in str(failure.value)
    assert "artifact=" in str(failure.value) and "stage=" in str(failure.value)
    assert failure.value.__cause__ is not None
    assert str(path) in caplog.text and _PRIVATE in caplog.text
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)
