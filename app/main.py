from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.context import router as context_router
from app.api.deps import get_workspace_container, workspace_container_scope
from app.api.jobs import router as jobs_router
from app.api.mcp import router as mcp_router
from app.api.message_stream import router as message_stream_router
from app.api.messages import router as messages_router
from app.api.node_debug import router as node_debug_router
from app.api.resources import router as resources_router
from app.api.runtime import router as runtime_router
from app.api.session_activity import router as session_activity_router
from app.api.session_navigation import router as session_navigation_router
from app.api.session_turns import router as session_turns_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router
from app.api.workspace_observation import router as workspace_observation_router
from app.backend_lifecycle import BackendLifespan
from app.container import WorkspaceContainerRegistry
from app.core.env import load_boxteam_env
from app.core.exceptions import BaseAPIException
from app.core.logging_config import configure_application_logging
from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.path_utils import (
    catalog_provider_scope,
    initialize_directories,
)
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.protocol.codecs.http_validation import request_validation_error_response
from app.runtime.model_stream import configure_model_stream_from_environment
from app.runtime.runtime_ports import DEFAULT_RUNTIME_CLOCK_PORTS
from app.schemas.internal_v2.sse import install_sse_openapi_components
from app.workspace_lifecycle import WorkspaceLifespan

configure_application_logging(service="backend")
load_boxteam_env()

# 导入期只冻结显式配置；SDK hook 在实际 Provider 请求前安装。
_model_stream_controller = configure_model_stream_from_environment(
    project_root=Path.cwd(),
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_application_logging(service="backend")
    startup_started = time.monotonic()
    logger.info("Backend 启动阶段 begin: stage=workspace_registry")
    if application.state.workspace_lifespan is not None:
        raise RuntimeError("原Backend生命周期尚未关闭，须先完成原关闭入口")
    lifecycle = BackendLifespan(_model_stream_controller, application)
    application.state.workspace_lifespan = lifecycle
    error: BaseException | None = None
    try:
        # 业务初始化前取得全部共享锁，只有原writer全部排空后才归还。
        mounted = MountedWorkspaceRegistry.from_environment()
        registry = WorkspaceContainerRegistry(
            mounted_workspaces=mounted, project_root=Path.cwd(), clock_ports=application.state.clock_ports,
        )
        lifecycle.registry = registry
        lifecycle.acquire_access()
        application.state.workspace_container_registry = registry
        logger.info("Backend 启动阶段 end: stage=workspace_registry workspace_ids=%s elapsed=%.3fs",
                    registry.workspace_ids, time.monotonic() - startup_started)
        logger_config: tuple[str, bool] | None = None
        for workspace_id in registry.workspace_ids:
            mount = mounted.resolve(workspace_id)
            mount_started = time.monotonic()
            logger.info("Workspace 挂载 begin: workspace_id=%s", workspace_id)
            with catalog_provider_scope(registry):
                initialize_directories(mount.workspace_id, mount.workspace_root)
                container = registry.get(workspace_id)
            # 先登记已构造容器，再检查配置；校验失败不能遗留资源。
            workspace = WorkspaceLifespan(container)
            lifecycle.workspaces.append(workspace)
            await workspace.start()
            logger.info("Workspace 挂载 ready: workspace_id=%s elapsed=%.3fs",
                        workspace_id, time.monotonic() - mount_started)
            candidate = (
                container.config_service.get_logger_level(),
                container.config_service.get_logger_pretty(),
            )
            if logger_config is not None and candidate != logger_config:
                raise RuntimeError(
                    "同一 Backend 进程的已挂载工作区必须使用相同日志配置: "
                    f"workspace_id={workspace_id}, expected={logger_config}, actual={candidate}"
                )
            logger_config = candidate
        assert logger_config is not None
        configure_application_logging(
            service="backend", level=logger_config[0], pretty=logger_config[1]
        )
        logger.info("已启动全部挂载工作区: workspace_ids=%s elapsed=%.3fs",
                    registry.workspace_ids, time.monotonic() - startup_started)
        yield
    except BaseException as failure:
        error = failure
        raise
    finally:
        try:
            await lifecycle.close()
        except BaseException as close_error:
            if error is not None:
                raise BaseExceptionGroup("Backend生命周期及关闭均失败", [error, close_error]) from None
            raise


app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    lifespan=lifespan,
)
app.state.clock_ports = DEFAULT_RUNTIME_CLOCK_PORTS
app.state.workspace_container_registry = None
app.state.workspace_lifespan = None

# Add trace middleware
app.add_middleware(TraceMiddleware)

# 允许本地前端开发服务器通过浏览器跨域访问后端接口。
# TODO: 这会放宽为允许所有来源，仅适合本地开发；若后续引入生产部署，请改成可配置项。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(BaseAPIException)
async def workspace_base_api_exception_handler(
    request: Request,
    error: BaseAPIException,
) -> JSONResponse:
    """``BaseAPIException`` 家族冒泡时的唯一收口点。

    ``ForbiddenError``/``NotFoundError`` 继承 ``HTTPException``；若某路由漏接，
    异常会冒泡到这里。没有本处理器时 Starlette 会命中上面的 ``HTTPException``
    处理器，把内部 ``detail`` 字典原样下发（历史实现还会带 500 状态码）。

    这里按异常自身的语义状态码与 ``readable_message()`` 下发，使「任何
    ``BaseAPIException`` 冒泡」都得到 4xx + 纯文本 detail，与路由显式映射
    （``not_found_http_error`` / ``forbidden_http_error``）复用同一份状态码与
    文本抽取，结构性消除「漏接即 500 + 泄漏」。

    Starlette 按异常类型的具体程度派发处理器，故本处理器优先于
    ``HTTPException`` 通用处理器，且不影响其它 ``HTTPException``。
    """
    request_id = get_request_id(request)
    response_headers = {
        key: value
        for key, value in (error.headers or {}).items()
        if key.lower() != "x-request-id"
    }
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=error.status_code,
        headers=response_headers,
        content={"detail": error.readable_message(), "request_id": request_id},
    )


@app.exception_handler(HTTPException)
async def workspace_http_exception_handler(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    """让工作区 API 的 HTTP 错误也遵守 request_id 响应约定。"""
    request_id = get_request_id(request)
    # HTTPException.headers 允许业务错误附带 WWW-Authenticate 等协议 header。
    # 复制后只覆盖可能伪造/过期的 X-Request-ID，保证异常响应自身也满足
    # body.request_id == headers.X-Request-ID；TraceMiddleware 返回时会再次写入同一值。
    response_headers = {
        key: value
        for key, value in (error.headers or {}).items()
        if key.lower() != "x-request-id"
    }
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=error.status_code,
        headers=response_headers,
        content={
            "detail": error.detail,
            "request_id": request_id,
        },
    )


app.add_exception_handler(RequestValidationError, request_validation_error_response)


workspace_api = APIRouter(dependencies=[Depends(workspace_container_scope)])


@workspace_api.get("/health", summary="健康检查")
async def health(request: Request):
    container = get_workspace_container(request)
    errors = container.runtime_service.startup_reconciliation_errors
    return {
        "status": "degraded" if errors else "ok",
        "process_id": os.getpid(),
        "workspace_id": container.workspace_id,
        "backend_generation": os.environ.get("BOXTEAM_BACKEND_GENERATION"),
        "mount_manifest_digest": request.app.state.workspace_container_registry.mounted_workspaces.manifest_digest,
        "config_proof": container.config_service.get_loaded_config_proof(),
        "startup_reconciliation": {"errors": errors},
    }


for router in (
    workspace_router,
    session_activity_router,
    workspace_observation_router,
    runtime_router,
    resources_router,
    sessions_router,
    session_turns_router,
    session_navigation_router,
    messages_router,
    context_router,
    mcp_router,
    jobs_router,
    agents_router,
    tools_router,
    artifacts_router,
    config_router,
    node_debug_router,
    message_stream_router,
):
    workspace_api.include_router(router)

# 两个载体均解析同一个显式 UUID；无 prefix 的调用必须带内部寻址头。
def workspace_path_identity(workspace_id: str) -> str:
    """把 canonical URL 的显式身份纳入 OpenAPI 参数合同。"""
    return workspace_id


app.include_router(
    workspace_api,
    prefix="/api/v1/workspaces/{workspace_id}",
    dependencies=[Depends(workspace_path_identity)],
)
app.include_router(workspace_api, prefix="/api/v1", include_in_schema=False)
install_sse_openapi_components(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
