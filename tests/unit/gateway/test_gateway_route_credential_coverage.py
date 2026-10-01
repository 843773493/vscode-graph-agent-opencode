"""Gateway 自有路由的本地/联邦凭据 fail-closed 护栏。

控制面私有数据（工作区路径、目录组织、生成器定义）只允许携带本机 Gateway 凭据的
调用方读取。`list_workspaces` 与刚收口的控制面导航端点属同一失败模式：它把每个
工作区的 root_path（客户端绝对路径）暴露给任意无凭据调用方。

本文件包含两类断言：
1. 端点级回归：无凭据 / 错误凭据访问 `GET /api/gateway/workspaces` 必须 401。
2. 机械化护栏：遍历 Gateway 自有路由（排除公开健康检查、按路由名声明为无鉴权的
   上下文读取，以及 WebSocket 与静态 attach 资源），断言每个端点都挂了本地/联邦
   凭据依赖，防止再次漏挂。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.trace_middleware import TraceMiddleware
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes.workspaces import router as workspaces_router


def _build_app(tmp_path: Path) -> FastAPI:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    application.state.registry = registry
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_default",
            name="默认工作区",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:9",
            connection_kind="local",
            owner="system",
            managed=False,
            system_default=True,
        )
    )
    application.include_router(workspaces_router)
    return application


@pytest.fixture
def gateway_app(tmp_path: Path) -> Iterator[FastAPI]:
    yield _build_app(tmp_path)


def test_list_workspaces_requires_local_token(gateway_app: FastAPI) -> None:
    """工作区列表暴露 root_path，缺凭据必须 401。"""

    with TestClient(gateway_app) as client:
        response = client.get("/api/gateway/workspaces?check_health=false")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid local token"


def test_list_workspaces_rejects_wrong_local_token(gateway_app: FastAPI) -> None:
    with TestClient(gateway_app) as client:
        response = client.get(
            "/api/gateway/workspaces?check_health=false",
            headers={"X-Local-Token": "wrong-token"},
        )

    assert response.status_code == 401


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


# 按路由名声明为设计无鉴权的自有路由（见各自 docstring）：
# - health：存活探针；
# - local_credential：同站点限制替代 token，本身就是取 token 的入口；
# - current_gateway_user：无 cookie 时建立游客态；
# - get_gateway_ui_asset：用户访问上下文；
# - proxy_context_read/search：docstring 明确「不要求 Gateway 凭据」，被集成测试固化。
_DELIBERATELY_UNPROTECTED_ENDPOINTS = frozenset(
    {
        "health",
        "local_credential",
        "current_gateway_user",
        "get_gateway_ui_asset",
        "proxy_context_read",
        "proxy_context_search",
    }
)


def test_every_gateway_route_mounts_a_credential_dependency() -> None:
    """机械化收口：Gateway 自有 HTTP 路由都必须挂本地/联邦凭据依赖。

    只豁免按路由名显式声明为设计无鉴权的端点。新增端点若忘记挂依赖会在此红掉，
    而非在真实部署里静默泄漏控制面数据。
    """

    from app.gateway.main import app as gateway_module_app

    unprotected: list[str] = []
    for route in gateway_module_app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue  # WebSocket / Mount 等非 HTTP 方法路由不在本护栏范围。
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        endpoint_name = getattr(endpoint, "__name__", "")
        if endpoint_name in _DELIBERATELY_UNPROTECTED_ENDPOINTS:
            continue
        if path.startswith("/api/gateway/attach/"):
            # attach 静态前端资源与协议资源按设计无鉴权（见 auxiliary_proxy）。
            continue
        if path in {
            "/api/gateway/openapi.json",
            "/api/gateway/docs",
            "/api/gateway/redoc",
            "/docs/oauth2-redirect",
        }:
            # FastAPI 自带的文档路由，非业务端点。
            continue
        names = _dependant_dependency_names(route)
        if not (
            names
            & {"verify_gateway_token", "verify_gateway_access", "verify_federation_token"}
        ):
            unprotected.append(f"{','.join(sorted(methods))} {path}")

    assert unprotected == []
