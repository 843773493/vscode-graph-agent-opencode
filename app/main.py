from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.context import router as context_router
from app.api.jobs import router as jobs_router
from app.api.mcp import router as mcp_router
from app.api.message_stream import router as message_stream_router
from app.api.messages import router as messages_router
from app.api.node_debug import router as node_debug_router
from app.api.runtime import router as runtime_router
from app.api.session_activity import router as session_activity_router
from app.api.session_navigation import router as session_navigation_router
from app.api.session_turns import router as session_turns_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router
from app.container import build_app_container
from app.core.env import load_boxteam_env
from app.core.logging_config import configure_application_logging
from app.core.path_utils import get_runtime_workspace_root
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.sse import install_sse_openapi_components
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.testing.model_stream import install_model_stream_from_environment

load_boxteam_env()

# 测试 transport 必须在 LiteLLM 第一次创建 async client 前安装；未显式配置时保持生产路径。
_model_stream_controller = install_model_stream_from_environment(
    project_root=Path.cwd(),
)

logger = logging.getLogger(__name__)


async def _prune_workspace_activity_periodically(container) -> None:
    while True:
        await asyncio.sleep(3600)
        removed = container.workspace_activity_service.prune()
        if removed:
            logger.info("清理 Workspace 会话活动事件: removed=%s", removed)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.core import path_utils

    configure_application_logging()
    logger.info("工作区后端日志引导已初始化")
    path_utils.initialize_directories()
    workspace_root = os.environ.get("WORKSPACE_ROOT", "") or None
    container = build_app_container(
        project_root=Path.cwd(),
        workspace_root=workspace_root,
    )
    _.state.container = container

    migrated_pending_files = await container.pending_request_store.migrate_all()
    if migrated_pending_files:
        logger.info(
            "已升级 %s 个旧 pending request 存储文件",
            migrated_pending_files,
        )

    container.config_service.validate_workspace_config()
    logger_level = container.config_service.get_logger_level()
    logger_pretty = container.config_service.get_logger_pretty()
    configure_application_logging(
        level=logger_level,
        pretty=logger_pretty,
    )
    logger.info(
        "工作区后端日志已初始化: level=%s pretty=%s workspace=%s",
        logger_level,
        logger_pretty,
        workspace_root or path_utils.get_runtime_workspace_root(),
    )

    await container.mcp_runtime_manager.start()
    try:
        removed_activity_events = container.workspace_activity_service.prune()
        if removed_activity_events:
            logger.info(
                "启动时清理 Workspace 会话活动事件: removed=%s",
                removed_activity_events,
            )
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
        await container.job_event_bus.register_durable_listener(
            container.goal_runtime_service.on_event
        )

        async def record_session_activity(event) -> None:
            if event.type not in {"job_completed", "job_failed", "job_cancelled"}:
                return
            job = await container.job_service.get(event.job_id)
            status = {
                "job_completed": "completed",
                "job_failed": "failed",
                "job_cancelled": "cancelled",
            }[event.type]
            if status != "completed":
                container.checkpointer.mark_turn_terminal_status(
                    session_id=job.session_id,
                    turn_id=job.job_id,
                    status=status,
                )
            summary = {
                "completed": "任务完成",
                "failed": "任务失败",
                "cancelled": "任务已取消",
            }[status]
            await container.workspace_activity_service.append(
                event_id=event.event_id,
                session_id=job.session_id,
                status=status,
                summary=summary,
                occurred_at=event.timestamp.isoformat(),
            )

        await container.job_event_bus.register_durable_listener(record_session_activity)
        reconciled_jobs = await container.runtime_service.reconcile_stale_executions()
        if reconciled_jobs:
            logger.warning(
                "检测到 %s 个上次进程未正常结束的 Job，已持久化为中断状态",
                reconciled_jobs,
            )
        await container.session_generation_service.start()
        await container.terminal_steering_service.start()
        await container.goal_runtime_service.resume_active_goals()
        activity_prune_task = asyncio.create_task(
            _prune_workspace_activity_periodically(container)
        )
        try:
            yield
        finally:
            activity_prune_task.cancel()
            await asyncio.gather(activity_prune_task, return_exceptions=True)
            await container.node_debug_service.close()
            await container.terminal_steering_service.shutdown()
            await container.session_generation_service.shutdown()
            await container.job_event_bus.unregister_durable_listener(
                container.goal_runtime_service.on_event
            )
            await container.job_event_bus.unregister_durable_listener(
                record_session_activity
            )
            await container.config_service.stop_watching()
            await container.workspace_file_watch_service.shutdown()
            await container.tool_test_service.shutdown()
            await container.trace_event_recorder.stop()
            container.workspace_activity_service.close()
    finally:
        await container.mcp_runtime_manager.shutdown()
        if _model_stream_controller is not None:
            await _model_stream_controller.aclose()
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
    # Gateway 重启时据此校验旧 Workspace API 仍属于同一个工作区，再安全接管。
    return {
        "status": "ok",
        "process_id": os.getpid(),
        "workspace_root": str(get_runtime_workspace_root()),
    }


app.include_router(workspace_router, prefix="/api/v1")
app.include_router(session_activity_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(session_turns_router, prefix="/api/v1")
app.include_router(session_navigation_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(context_router, prefix="/api/v1")
app.include_router(mcp_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(message_stream_router, prefix="/api/v1")
app.include_router(node_debug_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(tools_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")
install_sse_openapi_components(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
