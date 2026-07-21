from __future__ import annotations
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.trace_middleware import TraceMiddleware
from app.container import build_app_container
from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.jobs import router as jobs_router
from app.api.messages import router as messages_router
from app.api.mcp import router as mcp_router
from app.api.runtime import router as runtime_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router
from app.core.env import load_project_env
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)

load_project_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.core import path_utils

    path_utils.initialize_directories()
    workspace_root = os.environ.get("WORKSPACE_ROOT", "") or None
    container = build_app_container(
        project_root=Path.cwd(),
        workspace_root=workspace_root,
    )
    _.state.container = container

    container.config_service.validate_boxteam_config()
    logging.getLogger().setLevel(container.config_service.get_logger_level())

    await container.mcp_runtime_manager.start()
    try:
        container.config_service.set_mcp_tool_names(
            container.mcp_runtime_manager.get_tool_ids()
        )

        async def apply_config_candidate(
            previous: ConfigSnapshot,
            candidate: ConfigSnapshot,
        ) -> None:
            previous_config = container.config_service.config_from_snapshot(previous)
            candidate_config = container.config_service.config_from_snapshot(candidate)
            restart_sections = tuple(
                section
                for section in ("mcp", "logger")
                if previous_config.get(section, {}) != candidate_config.get(section, {})
            )
            if restart_sections:
                # TODO: MCP session 的 AnyIO cancel scope 要求在创建它的同一 Task
                # 中关闭。后续应引入带 generation lease 的专属 supervisor，
                # 在运行中 Job 排空后回收旧连接，再开放 MCP 配置热切换。
                raise ConfigRestartRequiredError(
                    "候选配置包含需要重启工作区后端的 section: "
                    + ", ".join(restart_sections),
                    changed_sections=restart_sections,
                )
            container.config_service.validate_candidate(
                candidate,
                mcp_tool_names=container.mcp_runtime_manager.get_tool_ids(),
            )

        await container.config_service.start_watching(
            candidate_applier=apply_config_candidate,
        )
        await container.trace_event_recorder.start()
        reconciled_jobs = await container.runtime_service.reconcile_stale_executions()
        if reconciled_jobs:
            logging.warning(
                "检测到 %s 个上次进程未正常结束的 Job，已持久化为中断状态",
                reconciled_jobs,
            )
        try:
            yield
        finally:
            await container.config_service.stop_watching()
            await container.tool_test_service.shutdown()
            await container.trace_event_recorder.stop()
    finally:
        await container.mcp_runtime_manager.shutdown()
        _.state.container = None

app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    lifespan=lifespan,
)

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


@app.get("/api/v1/health", summary="健康检查")
async def health():
    return {"status": "ok"}

app.include_router(workspace_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(mcp_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(tools_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
