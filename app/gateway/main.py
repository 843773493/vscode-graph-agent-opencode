"""BoxTeam Workspace Gateway 应用装配（路由实现见 app/gateway/routes/）。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.lifespan import lifespan
from app.gateway.route_registry import register_gateway_routes
from app.gateway.server.static_ui import install_static_web_ui

logger = logging.getLogger(__name__)


app = FastAPI(
    title="BoxTeam Workspace Gateway",
    version="1.0.0",
    docs_url="/api/gateway/docs",
    openapi_url="/api/gateway/openapi.json",
    redoc_url="/api/gateway/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TraceMiddleware)


@app.exception_handler(HTTPException)
async def gateway_http_exception_handler(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    """让依赖注入阶段的错误也遵守 Gateway request_id 响应约定。"""
    request_id = get_request_id(request)
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={
            "detail": error.detail,
            "request_id": request_id,
        },
    )


register_gateway_routes(app)

# 静态 UI 必须最后挂载，确保 Gateway API、工作区代理、SSE 和 WebSocket
# 路由优先匹配；源码开发未声明 BOXTEAM_WEB_ASSETS 时由 Vite 提供页面。
install_static_web_ui(app)
