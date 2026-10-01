"""冻结 ``POST /session-catalog/folders`` 的失败分类与同族三入口一致。

``create_folder`` 原先走只返回原始 record 的**单条** ``_submit``，不经
``_submit_batch`` 的 ``_raise_for_rejected`` 归口，导致被拒 operation 以裸
``RuntimeError`` 冒泡成 500（隔离副本实测 ``error_code=node_not_found`` 却回 500）。
本文件分两层锁住修复后的唯一归口：

1. 真实 SQLite catalog + 真实 ``SessionCatalogService`` + 真实 HTTP 封套，证明
   客户端传不存在的父节点落 404 且 detail 无 Python repr 引号；
2. handler 级 stub 覆盖，证明 ``ValueError``（形态/语义冲突）与 ``RuntimeError``
   （在途导航冲突）都归一到 409，且空目录创建仍是 200，成功契约不变。

按 tests/unit/api 规范：依赖覆盖隔离 + 不启动真实后端进程。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session_catalog_service, verify_local_token
from app.main import app
from app.schemas.internal_v2.session_navigation import SessionFolderCreateRequest
from app.services.business.session_navigation import SessionCatalogService
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


class _RejectingCatalogService:
    """按指定异常模拟 create_folder 的服务端拒绝。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create_folder(self, payload: SessionFolderCreateRequest):
        raise self._error


@pytest.fixture()
def service(tmp_path: Path) -> SessionCatalogService:
    workspace = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield SessionCatalogService(
            session_service=_SessionServiceFacade(workspace.resolver)
        )
    finally:
        workspace.close()


def _client(service: object) -> TestClient:
    app.dependency_overrides[get_session_catalog_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "local"
    return TestClient(app, raise_server_exceptions=False)


def test_create_folder_unknown_parent_maps_to_404_not_500(
    service: SessionCatalogService,
) -> None:
    """客户端传不存在的父节点 → 404 + 纯文本 detail（原 500 + RuntimeError 类名）。"""
    client = _client(service)
    try:
        response = client.post(
            "/api/v1/session-catalog/folders",
            json={"name": "子目录", "parent_folder_id": MISSING_NODE},
            headers={"X-Request-ID": "req_create_missing"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"会话目录节点不存在: {MISSING_NODE}",
        "request_id": "req_create_missing",
    }
    assert "RuntimeError" not in response.text


@pytest.mark.parametrize(
    ("error", "expected_detail"),
    [
        (ValueError("目标节点不是会话文件夹: f1"), "目标节点不是会话文件夹: f1"),
        (RuntimeError("导航 operation 被拒绝"), "导航 operation 被拒绝"),
    ],
)
def test_create_folder_rejection_maps_to_409(
    error: Exception,
    expected_detail: str,
) -> None:
    """形态/语义冲突与在途导航冲突都落 409，与同族三入口同一分类。"""
    client = _client(_RejectingCatalogService(error))
    try:
        response = client.post(
            "/api/v1/session-catalog/folders",
            json={"name": "子目录"},
            headers={"X-Request-ID": "req_create_conflict"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json() == {
        "detail": expected_detail,
        "request_id": "req_create_conflict",
    }


def test_create_folder_success_returns_root_page(service: SessionCatalogService) -> None:
    """正常创建仍是 200 + 根页，修复不改变成功契约。"""
    client = _client(service)
    try:
        response = client.post(
            "/api/v1/session-catalog/folders",
            json={"name": "正常目录"},
            headers={"X-Request-ID": "req_create_ok"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"]["items"][-1]["name"] == "正常目录"
