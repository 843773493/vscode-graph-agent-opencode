"""工作区生命周期按依赖顺序关闭；失败保留原步骤与存储供同入口重试。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Self

from app.core.lifecycle import LifetimeScope
from app.core.path_utils import workspace_context
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.event import Event
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.services.infrastructure.config.contracts import ConfigPublication
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.state import changed_json_paths

if TYPE_CHECKING:
    from app.container import AppContainer

logger = logging.getLogger(__name__)
CloseAction = Callable[[], Awaitable[None] | None]

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


async def _collect_attachment_directories_periodically(container: AppContainer) -> None:
    """目录按持久到期索引低频回收；失败保留原状态，下一周期继续核实。"""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            container.attachment_blob_store.reconcile_runtime_bindings(
                messages=container.checkpointer,
                is_current=container.thread_residency_tracker.is_current_runtime_binding,
            )
            await container.attachment_blob_store.collect_garbage(leases=container.external_resource_leases)
        except Exception:
            logger.exception("附件目录回收未收敛，保留关联与目录供下一周期重试")


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


class WorkspaceLifespan:
    def __init__(self, container: AppContainer) -> None:
        self.container = container
        self._periodic_tasks: list[asyncio.Task[None]] = []
        self._close_requested = asyncio.Event()
        self._close_result: asyncio.Future[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._steps: deque[tuple[str, CloseAction]] = deque([
            ("navigation_operations.stop", container.session_catalog_service.operations.stop),
            ("jobs.close_admission", partial(_drain_workspace_jobs, container)),
            ("jobs.drain_finalizations", container.job_service.drain_finalizations),
            ("node_debug.close", container.node_debug_service.close),
            ("terminal_steering.shutdown", container.terminal_steering_service.shutdown),
            ("session_generation.shutdown", container.session_generation_service.shutdown),
            ("activity.unregister", partial(container.job_event_bus.unregister_durable_listener, self.record_session_activity)),
            ("thread_initial_execution.unregister", partial(container.job_event_bus.unregister_durable_listener, container.thread_initial_execution_service.on_event)),
            ("thread_initial_execution.stop", container.thread_initial_execution_service.stop),
            ("communication.unregister", partial(container.job_event_bus.unregister_durable_listener, container.session_communication_service.observations.on_event)),
            ("communication.stop", container.session_communication_service.stop),
            ("goal_runtime.shutdown", container.goal_runtime_service.shutdown),
            ("goal_runtime.unregister", partial(container.job_event_bus.unregister_durable_listener, container.goal_runtime_service.on_event)),
            ("config.close", container.config_service.close),
            ("background_tasks.stop", container.background_task_registry.stop),
            ("team_fanout.stop", container.team_fanout_service.stop),
            ("runtime.shutdown", container.agent_execution_service.shutdown),
            ("resource_platform.close", container.resource_platform.close),
            ("tool_test.shutdown", container.tool_test_service.shutdown),
            ("trace_recorder.stop", container.trace_event_recorder.stop),
            ("activity.close", container.workspace_activity_service.close),
            ("mcp_catalog.shutdown", container.mcp_catalog_owner.shutdown),
            ("workspace_service.stop", container.workspace_service.stop),
            ("context_transport.close", container.workspace_session_context_transport.aclose),
            ("workspace_service.close", container.workspace_service.close),
            ("workspace_source.close", container.workspace_source_owner.close),
            ("attachments.close", container.attachment_blob_store.close),
        ])

    @property
    def closed(self) -> bool:
        return not self._steps and not self._periodic_tasks

    @property
    def pending_close_stage(self) -> str:
        if self._periodic_tasks:
            return "periodic_tasks"
        return self._steps[0][0] if self._steps else "closed"

    async def start(self) -> None:
        if self._owner is not None:
            raise RuntimeError("WorkspaceLifespan不能重复启动")
        self._ready = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        # MCP的AnyIO scope从创建到重试关闭始终留在同一任务。
        self._owner = asyncio.create_task(self._run(), name=f"workspace-lifespan:{self.container.workspace_id}")
        self._owner.add_done_callback(self._report_owner_failure)
        await asyncio.shield(self._ready)

    def _report_owner_failure(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException as error:
            for result in (self._ready, self._close_result):
                if result is not None and not result.done():
                    result.set_exception(error)
            logger.error("Workspace生命周期owner失败: task=%s", task.get_name(),
                         exc_info=(type(error), error, error.__traceback__))

    async def _run(self) -> None:
        if self._ready is None:
            raise RuntimeError("Workspace生命周期缺少启动回执")
        try:
            await self._start()
        except Exception as error:  # noqa: BLE001 - 原启动错误交给调用方，原owner仍接管关闭。
            self._ready.set_exception(error)
        else:
            self._ready.set_result(None)
        while True:
            await self._close_requested.wait()
            self._close_requested.clear()
            result = self._close_result
            if result is None:
                raise RuntimeError("Workspace生命周期缺少关闭回执")
            try:
                await self._close()
            except BaseException as error:
                result.set_exception(error)
                if isinstance(error, asyncio.CancelledError):
                    raise
            else:
                result.set_result(None)
                return

    async def close(self) -> None:
        if self.closed:
            if self._owner is not None:
                await self._owner
            return
        if self._owner is None:
            raise RuntimeError("Workspace生命周期尚未启动")
        if self._owner.done():
            await self._owner
            raise RuntimeError("Workspace生命周期owner已退出但仍有未关闭步骤")
        if self._close_result is None or self._close_result.done():
            self._close_result = asyncio.get_running_loop().create_future()
            self._close_result.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            self._close_requested.set()
        await asyncio.shield(self._close_result)
        await self._owner

    async def _close(self) -> None:
        # TODO(5.4)：同步阻塞与外部终止核实仍待收口；Backend到期不取消原步骤。
        with workspace_context(self.container.workspace_id, self.container.workspace_root,
                               session_catalog=self.container.session_catalog):
            while self._periodic_tasks:
                await _stop_activity_pruning(self._periodic_tasks[-1])
                self._periodic_tasks.pop()
            while self._steps:
                stage, action = self._steps[0]
                try:
                    result = action()
                    if inspect.isawaitable(result):
                        await result
                except BaseException:
                    logger.exception("Workspace关闭未完成，保留当前步骤及后续依赖: workspace_id=%s stage=%s",
                                     self.container.workspace_id, stage)
                    raise
                self._steps.popleft()

    async def __aenter__(self) -> Self:
        try:
            await self.start()
        except BaseException as error:
            try:
                await self.close()
            except BaseException as close_error:  # noqa: BLE001 - 保留启动和清理原错。
                raise BaseExceptionGroup("工作区启动及关闭均失败", [error, close_error]) from None
            raise
        return self

    async def __aexit__(self, _kind, error, _traceback) -> None:
        try:
            await self.close()
        except BaseException as close_error:
            if error is not None:
                raise BaseExceptionGroup("工作区生命周期及关闭均失败", [error, close_error]) from None
            raise

    async def record_session_activity(self, event: Event) -> None:
        if event.type not in {"job_completed", "job_failed", "job_cancelled"}:
            return
        job = await self.container.job_service.get(event.job_id)
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
        await self.container.workspace_activity_service.append(
            event_id=event.event_id,
            session_id=job.session_id,
            status=status,
            summary=summary,
            occurred_at=event.timestamp.isoformat(),
        )


    async def _start(self) -> None:
        container = self.container
        operations = container.session_catalog_service.operations
        with workspace_context(container.workspace_id, container.workspace_root, session_catalog=container.session_catalog):
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
                scope: LifetimeScope,
            ) -> ConfigPublication:
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
                    raise ConfigRestartRequiredError(
                        "候选配置包含需要重启工作区后端的 section: "
                        + ", ".join(restart_sections),
                        changed_sections=restart_sections,
                    )
                activate_mcp: ConfigPublication = lambda: None
                mcp_tool_names = container.mcp_catalog_owner.get_tool_ids()
                if previous_config.get("mcp") != candidate_config.get("mcp"):
                    activate_mcp, mcp_tool_names = await container.mcp_catalog_owner.prepare_config(
                        candidate_config.get("mcp"), scope,
                    )
                container.config_service.validate_candidate(candidate, mcp_tool_names=mcp_tool_names)

                def activate() -> None:
                    activate_mcp()
                    container.config_service.set_mcp_tool_names(mcp_tool_names)

                return activate

            with _workspace_startup_stage(container, "config_service.start_watching"):
                await container.config_service.start_watching(
                    candidate_applier=apply_config_candidate,
                    shadow_reconcile=container.resource_platform.prepare_config,
                )
            with _workspace_startup_stage(container, "trace_event_recorder.start"):
                await container.trace_event_recorder.start()
            with _workspace_startup_stage(container, "job_event_bus.register_durable_listeners"):
                await container.job_event_bus.register_durable_listener(container.goal_runtime_service.on_event)
                await container.job_event_bus.register_durable_listener(container.thread_initial_execution_service.on_event)
                await container.job_event_bus.register_durable_listener(self.record_session_activity)
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
            with _workspace_startup_stage(container, "attachment_blob_store.reconcile_runtime_bindings"):
                container.attachment_blob_store.reconcile_runtime_bindings(
                    messages=container.checkpointer,
                    is_current=container.thread_residency_tracker.is_current_runtime_binding,
                )
            with _workspace_startup_stage(container, "attachment_blob_store.collect_garbage"):
                collected = await container.attachment_blob_store.collect_garbage(leases=container.external_resource_leases)
            if collected:
                logger.info("已按附件 catalog 回收 %s 个到期目录", len(collected))
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
            self._periodic_tasks.append(task)
            browser_sweep = asyncio.create_task(
                _sweep_browser_orphans_periodically(container),
                name=f"browser-orphan-reclaim:{container.workspace_id}",
            )
            self._periodic_tasks.append(browser_sweep)
            attachment_gc = asyncio.create_task(
                _collect_attachment_directories_periodically(container),
                name=f"attachment-directory-reclaim:{container.workspace_id}",
            )
            self._periodic_tasks.append(attachment_gc)
            fork_export_cleanup = asyncio.create_task(
                _retry_pending_fork_exports_periodically(container),
                name=f"fork-export-cleanup:{container.workspace_id}",
            )
            self._periodic_tasks.append(fork_export_cleanup)
