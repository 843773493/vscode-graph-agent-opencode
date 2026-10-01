"""Gateway 路由注册顺序（顺序即契约）。"""

from __future__ import annotations

from fastapi import FastAPI

from app.gateway.auxiliary_proxy import router as auxiliary_proxy_router
from app.gateway.control.router import router as gateway_control_router
from app.gateway.device_connections import router as device_connections_router
from app.gateway.federation.router import router as federation_router
from app.gateway.routes import (
    federation,
    health_config,
    ui_assets,
    users,
    workspaces,
    workspaces_lifecycle,
    workspaces_managed,
)
from app.gateway.server.port_forwarding import router as port_forwards_router
from app.gateway.server.workspace_proxy import router as workspace_proxy_router

# Gateway 自有路由组保持拆分前的定义顺序，避免 openapi 路径与路由匹配顺序漂移。
ROUTE_MODULES = (
    health_config,
    users,
    workspaces,
    federation,
    workspaces_managed,
    ui_assets,
    workspaces_lifecycle,
)


def register_gateway_routes(app: FastAPI) -> None:
    for module in ROUTE_MODULES:
        app.include_router(module.router)
    # 两个代理 Router 含通配路由，必须晚于 Gateway 自有接口注册，否则会吞掉
    # `/api/gateway/workspaces/{id}/runtime/*` 等更具体的控制面路由。
    app.include_router(gateway_control_router)
    app.include_router(device_connections_router)
    app.include_router(port_forwards_router)
    app.include_router(auxiliary_proxy_router)
    app.include_router(workspace_proxy_router)
    app.include_router(federation_router)
