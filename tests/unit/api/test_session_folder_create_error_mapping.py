"""验证会话文件夹写入使用 durable navigation operation。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import main
from app.api import session_navigation as navigation_api
from app.api.deps import get_session_catalog_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.queue_store import (
    NavigationMutationConflictError,
)
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
MISSING_NODE = "ses_00000000-0000-4000-8000-000000000000"


class _SessionServiceFacade:
    """SessionCatalogService 只需要 resolver 与 change-listener 注册面。"""

    def __init__(self, resolver) -> None:
        self.path_resolver = resolver
        self._listeners: list[object] = []

    def register_change_listener(self, listener: object) -> None:
        self._listeners.append(listener)

    async def get(self, session_id: str):
        raise KeyError(f"会话目录节点不存在: {session_id}")


class _RejectingQueueService:
    """按指定 admission 异常模拟 durable queue service。"""

    def __init__(self, error: Exception) -> None:
        self.workspace_id = WORKSPACE_ID
        self.error = error

    async def submit_operation_batch(self, payload, scope):
        raise self.error


@pytest.fixture()
def service(tmp_path: Path) -> SessionCatalogService:
    workspace = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield SessionCatalogService(
            session_service=_SessionServiceFacade(workspace.resolver)
        )
    finally:
        workspace.close()


def _client(service: object) -> tuple[FastAPI, TestClient]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        catalog = service if isinstance(service, SessionCatalogService) else None
        if catalog is not None:
            await catalog.operations.start()
        try:
            yield
        finally:
            if catalog is not None:
                await catalog.operations.stop()

    test_app = FastAPI(lifespan=lifespan)
    test_app.add_middleware(TraceMiddleware)
    test_app.add_exception_handler(HTTPException, main.workspace_http_exception_handler)
    test_app.include_router(navigation_api.router, prefix="/api/v1")
    test_app.dependency_overrides[get_session_catalog_service] = lambda: service
    test_app.dependency_overrides[verify_local_token] = lambda: "local"
    return test_app, TestClient(test_app, raise_server_exceptions=False)


def _headers(request_id: str) -> dict[str, str]:
    return {
        "X-Local-Token": "local-dev-token",
        "X-Request-ID": request_id,
        "X-BoxTeam-Gateway-Id": "gateway_test",
        "X-BoxTeam-Navigation-Actor": "user:test-user",
    }


def _create_folder_payload(
    *,
    operation_id: str,
    catalog_revision: int,
    name: str,
    parent_node_id: str | None = None,
) -> dict[str, object]:
    intent: dict[str, object] = {
        "client_operation_id": operation_id,
        "client_sequence": 1,
        "kind": "create_folder",
        "base_catalog_revision": catalog_revision,
        "name": name,
    }
    if parent_node_id is not None:
        intent["parent_node_id"] = parent_node_id
    return NavigationMutationEnqueueRequest.model_validate(
        {"intents": [intent]}
    ).model_dump(mode="json")


def _wait_for_terminal(client: TestClient, operation_id: str) -> dict[str, object]:
    for attempt in range(100):
        response = client.get(
            "/api/v1/session-catalog/operations",
            params={"operation_id": operation_id},
            headers=_headers(f"req_status_{attempt}"),
        )
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        if items and items[0]["state"] in {
            "committed",
            "rejected",
            "cancelled",
            "dependency_failed",
        }:
            return items[0]
        time.sleep(0.05)
    raise AssertionError(f"导航 operation 未到终态: {operation_id}")


def test_unknown_parent_is_reported_by_durable_rejection(
    service: SessionCatalogService,
) -> None:
    operation_id = "op_" + "a" * 32
    test_app, client = _client(service)
    try:
        with client:
            response = client.post(
                "/api/v1/session-catalog/operations:enqueue",
                json=_create_folder_payload(
                    operation_id=operation_id,
                    catalog_revision=service.path_resolver.revision,
                    name="子目录",
                    parent_node_id=MISSING_NODE,
                ),
                headers=_headers("req_create_missing"),
            )
            assert response.status_code == 202, response.text
            assert response.json()["request_id"] == "req_create_missing"
            assert response.json()["data"]["accepted_count"] == 1
            assert (
                response.json()["data"]["receipts"][0]["operation_id"] == operation_id
            )
            receipt = _wait_for_terminal(client, operation_id)
    finally:
        test_app.dependency_overrides.clear()

    assert receipt["state"] == "rejected"
    assert receipt["error_code"] == "node_not_found"
    assert receipt["error_detail"] == f"会话目录节点不存在: {MISSING_NODE}"
    assert "KeyError" not in str(receipt["error_detail"])


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (
            KeyError("会话目录 operation 不存在: op_missing"),
            400,
            "会话目录 operation 不存在: op_missing",
        ),
        (ValueError("导航 intent 形态非法"), 400, "导航 intent 形态非法"),
        (TypeError("导航 intent 类型非法"), 400, "导航 intent 类型非法"),
        (
            NavigationMutationConflictError("operation preimage 冲突"),
            409,
            "operation preimage 冲突",
        ),
    ],
)
def test_enqueue_preserves_admission_error_mapping(
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    test_app, client = _client(_RejectingQueueService(error))
    try:
        with client:
            response = client.post(
                "/api/v1/session-catalog/operations:enqueue",
                json=_create_folder_payload(
                    operation_id="op_" + "b" * 32,
                    catalog_revision=0,
                    name="冲突目录",
                ),
                headers=_headers("req_enqueue_error"),
            )
    finally:
        test_app.dependency_overrides.clear()

    assert response.status_code == expected_status
    assert response.json() == {
        "detail": expected_detail,
        "request_id": "req_enqueue_error",
    }


def test_create_folder_returns_durable_receipt_and_commits_to_catalog(
    service: SessionCatalogService,
) -> None:
    operation_id = "op_" + "c" * 32
    test_app, client = _client(service)
    try:
        with client:
            response = client.post(
                "/api/v1/session-catalog/operations:enqueue",
                json=_create_folder_payload(
                    operation_id=operation_id,
                    catalog_revision=service.path_resolver.revision,
                    name="正常目录",
                ),
                headers=_headers("req_create_ok"),
            )
            assert response.status_code == 202, response.text
            assert response.json()["request_id"] == "req_create_ok"
            receipt = _wait_for_terminal(client, operation_id)
            export_response = client.get(
                "/api/v1/session-catalog/export",
                headers=_headers("req_catalog_export"),
            )
    finally:
        test_app.dependency_overrides.clear()

    assert receipt["state"] == "committed"
    assert receipt["kind"] == "create_folder"
    assert export_response.status_code == 200, export_response.text
    folders = export_response.json()["data"]["items"]
    assert any(item["node_id"] == receipt["created_node_id"] for item in folders)
