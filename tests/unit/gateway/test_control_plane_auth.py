"""Gateway 控制面自有 API 的本地凭据 fail-closed 契约。

控制面会向调用方暴露工作区路径、会话目录组织与生成器定义等 Gateway 私密控制面
数据。这些数据只允许携带本机 Gateway 本地凭据的调用方读取；缺失或错误凭据必须
在依赖注入阶段就 fail-closed 返回 401，MUST NOT 静默返回 200 与业务数据。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.trace_middleware import TraceMiddleware
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.router import router as control_router
from app.gateway.registry import GatewayWorkspaceRegistry


def _build_app(tmp_path: Path) -> FastAPI:
    application = FastAPI()
    # 与真实 Gateway 一致：request_id 由 TraceMiddleware 注入，生产端点依赖它。
    application.add_middleware(TraceMiddleware)
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    application.state.registry = registry
    application.state.workspace_navigation_store = WorkspaceNavigationStore(
        storage_path=tmp_path / "navigation.json"
    )
    application.include_router(control_router)
    return application


@pytest.fixture
def gateway_app(tmp_path: Path) -> Iterator[FastAPI]:
    yield _build_app(tmp_path)


def test_workspace_navigation_requires_local_token(gateway_app: FastAPI) -> None:
    """控制面导航树暴露工作区路径与目录组织，缺凭据必须 401。"""

    with TestClient(gateway_app) as client:
        response = client.get("/api/gateway/workspace-navigation")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid local token"


def test_workspace_navigation_rejects_wrong_local_token(gateway_app: FastAPI) -> None:
    with TestClient(gateway_app) as client:
        response = client.get(
            "/api/gateway/workspace-navigation",
            headers={"X-Local-Token": "wrong-token"},
        )

    assert response.status_code == 401
