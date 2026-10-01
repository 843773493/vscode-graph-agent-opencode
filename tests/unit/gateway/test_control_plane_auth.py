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


# 同族收口：以下端点与导航树属同一失败模式（控制面私有数据未挂本地凭据依赖），
# 缺凭据必须在依赖注入阶段 fail-closed 返回 401，MUST NOT 返回 200 与业务数据。
_UNPROTECTED_CONTROL_PLANE_REQUESTS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", "/api/gateway/session-catalog/search?query=a", None),
    ("GET", "/api/gateway/session-generators", None),
    ("POST", "/api/gateway/session-generators/preview-placement", {"json": {}}),
    ("GET", "/api/gateway/session-generators/gen-1/runs", None),
    ("GET", "/api/gateway/workspace-navigation/nodes/node-1/breadcrumb", None),
)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    _UNPROTECTED_CONTROL_PLANE_REQUESTS,
    ids=[f"{method} {path.split('?')[0]}" for method, path, _ in _UNPROTECTED_CONTROL_PLANE_REQUESTS],
)
def test_control_plane_read_endpoints_require_local_token(
    gateway_app: FastAPI,
    method: str,
    path: str,
    kwargs: dict[str, object] | None,
) -> None:
    with TestClient(gateway_app) as client:
        response = client.request(method, path, **(kwargs or {}))

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "invalid local token"


def _dependant_dependency_names(route: object) -> set[str]:
    """递归收集路由依赖处理函数名，用于机械化审计凭据依赖是否挂全。"""

    names: set[str] = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        dependant = stack.pop()
        if dependant is None:
            continue
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
        stack.extend(getattr(dependant, "dependencies", []) or [])
    return names


def test_every_control_plane_route_mounts_gateway_credential_dependency() -> None:
    """机械化收口：本 router 下任何端点都必须挂本地/联邦凭据依赖。

    该断言是防止控制面端点再次漏挂凭据依赖的护栏；新增端点若忘记挂依赖会
    在此红掉，而非在真实部署里静默泄漏控制面数据。
    """

    unprotected: list[str] = []
    for route in control_router.routes:
        names = _dependant_dependency_names(route)
        if not (names & {"verify_gateway_token", "verify_gateway_access"}):
            methods = ",".join(sorted(getattr(route, "methods", []) or []))
            unprotected.append(f"{methods} {getattr(route, 'path', '?')}")

    assert unprotected == []
