from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import timedelta
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
from app.container import AppContainer, WorkspaceContainerRegistry
from app.core.env import load_boxteam_env
from app.core.exceptions import BaseAPIException
from app.core.logging_config import configure_application_logging
from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.path_utils import (
    catalog_provider_scope,
    initialize_directories,
    workspace_context,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.protocol.codecs.http_validation import request_validation_error_response
from app.runtime.model_stream import configure_model_stream_from_environment
from app.runtime.runtime_ports import DEFAULT_RUNTIME_CLOCK_PORTS
from app.schemas.internal_v2.sse import install_sse_openapi_components
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    ATTACHMENT_ROOT_DIRECTORY_NAME,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.state import changed_json_paths

load_boxteam_env()

# 导入期只冻结显式配置；SDK hook 在实际 Provider 请求前安装。
_model_stream_controller = configure_model_stream_from_environment(
    project_root=Path.cwd(),
)

logger = logging.getLogger(__name__)


@contextmanager
def _workspace_startup_stage(container: AppContainer, stage: str):
    started = time.monotonic()
    logger.info("Workspace 启动阶段 begin: workspace_id=%s stage=%s",
                container.workspace_id, stage)
    yield
    logger.info("Workspace 启动阶段 end: workspace_id=%s stage=%s elapsed=%.3fs",
                container.workspace_id, stage, time.monotonic() - started)


async def _prune_workspace_activity_periodically(container: AppContainer) -> None:
    while True:
        await asyncio.sleep(3600)
        removed = container.workspace_activity_service.prune()
        if removed:
            logger.info("清理 Workspace 会话活动事件: removed=%s", removed)


async def _stop_activity_pruning(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)



async def _sweep_browser_orphans_periodically(container: AppContainer) -> None:
    while True:
        try:
            await container.browser_resource_owner.sweep_orphans()
        except Exception:
            logger.exception("Browser owner 孤儿回收未收敛；资源仍保留阻断事实")
        await asyncio.sleep(1)


async def _retry_pending_fork_exports_periodically(container: AppContainer) -> None:
    while True:
        try:
            await container.session_context_fork_service.retry_pending_remote_exports()
        except Exception:
            # 来源传输异常由 owner 记录为 durable pending；逃逸到这里的是非法状态，必须暴露。
            logger.exception("fork export cleanup 遇到未处理错误，停止周期恢复")
            raise
        await asyncio.sleep(1)


async def _drain_workspace_jobs(container: AppContainer) -> None:
    await container.goal_runtime_service.close_admission()
    container.job_service.close_admission()
    await container.job_service.force_interrupt_active(reason="工作区后端正在关闭")


@asynccontextmanager
async def _workspace_lifespan(container: AppContainer):
    # 创建、启动和关闭 MCP cancel scope 必须在同一 lifespan task 内完成。
    with workspace_context(
        container.workspace_id,
        container.workspace_root,
        session_catalog=container.session_catalog,
    ):
        async with AsyncExitStack() as resources:
            # 先登记持有资源的关闭动作，部分启动失败也沿同一顺序排空。
            resources.callback(container.attachment_blob_store.close)
            resources.callback(container.workspace_source_owner.close)
            resources.callback(container.workspace_service.close)
            resources.push_async_callback(container.workspace_session_context_transport.aclose)
            resources.push_async_callback(container.workspace_service.stop)
            resources.push_async_callback(container.mcp_catalog_owner.shutdown)
            resources.callback(container.workspace_activity_service.close)
            resources.push_async_callback(container.trace_event_recorder.stop)
            resources.push_async_callback(container.tool_test_service.shutdown)
            resources.push_async_callback(container.resource_platform.close)
            resources.push_async_callback(container.agent_execution_service.shutdown)
            resources.push_async_callback(container.team_fanout_service.stop)
            resources.push_async_callback(container.background_task_registry.stop)
            resources.push_async_callback(container.config_service.close)
            resources.push_async_callback(
                container.job_event_bus.unregister_durable_listener,
                container.goal_runtime_service.on_event,
            )
            resources.push_async_callback(container.goal_runtime_service.shutdown)
            resources.push_async_callback(container.session_communication_service.stop)
            resources.push_async_callback(container.job_event_bus.unregister_durable_listener, container.session_communication_service.observations.on_event)
            resources.push_async_callback(container.thread_initial_execution_service.stop)
            resources.push_async_callback(container.job_event_bus.unregister_durable_listener,
                                          container.thread_initial_execution_service.on_event)

            async def record_session_activity(event) -> None:
                if event.type not in {"job_completed", "job_failed", "job_cancelled"}:
                    return
                job = await container.job_service.get(event.job_id)
                status = {
                    "job_completed": "completed",
                    "job_failed": "failed",
                    "job_cancelled": "cancelled",
                }[event.type]
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

            resources.push_async_callback(
                container.job_event_bus.unregister_durable_listener,
                record_session_activity,
            )
            resources.push_async_callback(container.session_generation_service.shutdown)
            resources.push_async_callback(container.terminal_steering_service.shutdown)
            resources.push_async_callback(container.node_debug_service.close)
            # 关闭准入并收口真实 Job 后，才能关闭 runtime owner 和资源平台。
            resources.push_async_callback(container.job_service.drain_finalizations)
            resources.push_async_callback(_drain_workspace_jobs, container)
            operations = container.session_catalog_service.operations
            resources.push_async_callback(operations.stop)

            with _workspace_startup_stage(container, "session_catalog.verify_registered_date_directories"):
                async with NavigationTopologyGate(container.session_catalog.sessions_root).shared():
                    await run_sync_shielded(
                        container.session_service.path_resolver.catalog_store.verify_registered_date_directories
                    )
            container.config_service.validate_workspace_config()
            with _workspace_startup_stage(container, "workspace_service.start"):
                await container.workspace_service.start()
            with _workspace_startup_stage(container, "mcp_catalog_owner.start"):
                await container.mcp_catalog_owner.start()
            with _workspace_startup_stage(container, "workspace_file_resource_registry.start"):
                await container.workspace_file_resource_registry.start()
            removed = container.workspace_activity_service.prune()
            if removed:
                logger.info("启动时清理 Workspace 会话活动事件: removed=%s", removed)
            container.config_service.set_mcp_tool_names(
                container.mcp_catalog_owner.get_tool_ids()
            )

            async def apply_config_candidate(
                previous: ConfigSnapshot,
                candidate: ConfigSnapshot,
            ) -> None:
                previous_config = container.config_service.config_from_snapshot(previous)
                candidate_config = container.config_service.config_from_snapshot(candidate)
                changed_paths = changed_json_paths(previous_config, candidate_config)
                restart_paths = workspace_config_policy().restart_paths(changed_paths)
                restart_sections = tuple(dict.fromkeys(
                    path.strip("/").split("/")[0]
                    for path in restart_paths
                    if path != "/"
                ))
                if restart_sections:
                    # TODO: MCP 热切换需要专属 supervisor 在原 task 排空旧 generation。
                    raise ConfigRestartRequiredError(
                        "候选配置包含需要重启工作区后端的 section: "
                        + ", ".join(restart_sections),
                        changed_sections=restart_sections,
                    )
                container.config_service.validate_candidate(
                    candidate,
                    mcp_tool_names=container.mcp_catalog_owner.get_tool_ids(),
                )

            with _workspace_startup_stage(container, "config_service.start_watching"):
                await container.config_service.start_watching(candidate_applier=apply_config_candidate)
            with _workspace_startup_stage(container, "trace_event_recorder.start"):
                await container.trace_event_recorder.start()
            with _workspace_startup_stage(container, "job_event_bus.register_durable_listeners"):
                await container.job_event_bus.register_durable_listener(container.goal_runtime_service.on_event)
                await container.job_event_bus.register_durable_listener(container.thread_initial_execution_service.on_event)
                await container.job_event_bus.register_durable_listener(record_session_activity)
            with _workspace_startup_stage(container, "pending_request_store.migrate_all"):
                await container.pending_request_store.migrate_all()
            with _workspace_startup_stage(container, "runtime_service.reconcile_stale_executions"):
                await container.checkpointer.recover_internal_interrupts()
                reconciled = await container.runtime_service.reconcile_stale_executions()
            if reconciled:
                logger.warning("已收敛 %s 项遗留 Job 与消息流执行状态", reconciled)
            with _workspace_startup_stage(container, "session_service.recover_context_controls"):
                controls = await container.session_service.recover_context_controls()
            if controls:
                logger.warning("已明确终结 %s 个崩溃遗留 Skill/ToolSet 控制 lease", controls)
            with _workspace_startup_stage(container, "session_context_fork_service.recover_prepared_forks"):
                recovered = await container.session_context_fork_service.recover_prepared_forks()
            if recovered:
                logger.info("已恢复 %s 个未发布 context fork", recovered)
            with _workspace_startup_stage(container, "attachment_blob_store.recover_pending_ingests"):
                recovered_ingests = await container.attachment_blob_store.recover_pending_ingests(messages=container.checkpointer)
            if recovered_ingests:
                logger.info("已按持久 operation 收敛 %s 个附件上传 pin", recovered_ingests)
            with _workspace_startup_stage(container, "attachment_blob_store.collect_garbage"):
                collected = await container.attachment_blob_store.collect_garbage(retention=timedelta(0))
            if collected:
                logger.info("已按附件 catalog 回收 %s 个零引用 blob", len(collected))
            with _workspace_startup_stage(container, "agent_execution_service.start"):
                await container.agent_execution_service.start()
            with _workspace_startup_stage(container, "team_fanout_service.start"):
                await container.team_fanout_service.start()
            with _workspace_startup_stage(container, "background_message_bus.reconcile"):
                await container.background_message_bus.reconcile(container.checkpointer)
            with _workspace_startup_stage(container, "background_task_registry.start"):
                await container.background_task_registry.start()
            with _workspace_startup_stage(container, "thread_initial_execution_service.start"):
                await container.thread_initial_execution_service.start()
            with _workspace_startup_stage(container, "job_event_bus.register_observation_listener"):
                await container.job_event_bus.register_durable_listener(container.session_communication_service.observations.on_event)
            with _workspace_startup_stage(container, "session_communication_service.start"):
                await container.session_communication_service.start()
            with _workspace_startup_stage(container, "runtime_source_dispatcher.start"):
                await container.runtime_source_dispatcher.start()
            with _workspace_startup_stage(container, "session_generation_service.start"):
                await container.session_generation_service.start()
            with _workspace_startup_stage(container, "terminal_steering_service.start"):
                await container.terminal_steering_service.start()
            with _workspace_startup_stage(container, "goal_runtime_service.resume_active_goals"):
                await container.goal_runtime_service.resume_active_goals()
            with _workspace_startup_stage(container, "navigation_operations.start"):
                await operations.start()
            logger.info(
                "导航 queue worker 已取得 owner 并完成恢复: workspace_id=%s state=%s",
                container.workspace_id,
                operations.worker_state,
            )
            with _workspace_startup_stage(container, "job_service.start_restored_executions"):
                resumed = await container.job_service.start_restored_executions()
            if resumed:
                logger.info("已从原 pending tools checkpoint 续接 %s 个 Job", resumed)
            task = asyncio.create_task(
                _prune_workspace_activity_periodically(container),
                name=f"workspace-activity-prune:{container.workspace_id}",
            )
            resources.push_async_callback(_stop_activity_pruning, task)
            browser_sweep = asyncio.create_task(
                _sweep_browser_orphans_periodically(container),
                name=f"browser-orphan-reclaim:{container.workspace_id}",
            )
            resources.push_async_callback(_stop_activity_pruning, browser_sweep)
            fork_export_cleanup = asyncio.create_task(
                _retry_pending_fork_exports_periodically(container),
                name=f"fork-export-cleanup:{container.workspace_id}",
            )
            resources.push_async_callback(_stop_activity_pruning, fork_export_cleanup)
            yield


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_application_logging()
    startup_started = time.monotonic()
    logger.info("Backend 启动阶段 begin: stage=workspace_registry")
    try:
        async with AsyncExitStack() as workspaces:
            if _model_stream_controller is not None:
                workspaces.push_async_callback(_model_stream_controller.aclose)
            mounted = MountedWorkspaceRegistry.from_environment()
            # 任何工作区业务初始化之前取得全部共享锁，排空全部 writer 后再归还。
            for mount in mounted.mounts:
                workspaces.enter_context(AttachmentMaintenanceLock(
                    mount.workspace_root / ".boxteam" / ATTACHMENT_ROOT_DIRECTORY_NAME,
                    exclusive=False,
                ))
            registry = WorkspaceContainerRegistry(
                mounted_workspaces=mounted,
                project_root=Path.cwd(),
                clock_ports=application.state.clock_ports,
            )
            workspaces.push(registry)
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
                await workspaces.enter_async_context(_workspace_lifespan(container))
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
                level=logger_config[0], pretty=logger_config[1]
            )
            logger.info("已启动全部挂载工作区: workspace_ids=%s elapsed=%.3fs",
                        registry.workspace_ids, time.monotonic() - startup_started)
            yield
    finally:
        application.state.workspace_container_registry = None


app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    lifespan=lifespan,
)
app.state.clock_ports = DEFAULT_RUNTIME_CLOCK_PORTS

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
