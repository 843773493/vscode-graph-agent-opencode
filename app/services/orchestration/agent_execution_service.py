from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextvars import Context
from pathlib import Path
from typing import Any, Final

from langchain_core.tools import BaseTool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_factory import resolve_agent_id
from app.agents.checkpoint_recovery import (
    CheckpointRecoveryRejectedError,
    persisted_pending_tool_candidate,
)
from app.agents.graph_binding import (
    GraphBindingOwnerKey,
    GraphBindingStorePort,
    resolve_graph_binding,
)
from app.agents.skill_catalog import (
    PublishedSkillCatalog,
    build_workspace_skill_catalog,
    start_workspace_skill_catalog_publication,
)
from app.agents.tools.custom_invocation import (
    seal_extension_catalog_binding_from_tools,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import (
    SessionCatalogComponents,
    get_session_path_resolver,
    workspace_context,
)
from app.core.phase_observer import observe_phase
from app.core.session_thread_context import session_thread_owner_scope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.trace_middleware import get_current_gateway_id
from app.core.turn_execution_scope import (
    TurnExecutionScopeRegistry,
)
from app.core.workspace_identity import validate_workspace_id
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.runtime.agent_runtime import (
    AgentRuntimeDependencyProvider,
    build_agent_tool_definitions,
    build_session_agent_runtime,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.business.job.service import JobState
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.message_stream_store import (
    MessageStreamStore,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.orchestration.event_stream.contracts import AgentEventSource
from app.services.orchestration.execution_step.ports import (
    StepAgentRuntime,
    StepExecutionPorts,
)
from app.services.orchestration.execution_step.runner import StepRunner
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.thread_residency import (
    ThreadResidencyTracker,
    ThreadRuntimeLease,
)

# 工具面 blueprint 检查用的合成会话：只用于装配一次完整工具面并抽取定义，
# 不进入任何业务会话账目。抽出的定义不捕获该会话，仅用于 Provider 工具目录。
_TOOL_INSPECTION_SESSION_ID: Final[str] = "tools_inspection_session"
logger = logging.getLogger(__name__)


class AgentExecutionService(JobStepExecutor):
    def __init__(
        self,
        *,
        config_service: ConfigService,
        background_task_registry: BackgroundTaskRegistry,
        background_message_bus: BackgroundMessageBusProtocol,
        job_event_bus: JobEventBusProtocol,
        dependency_provider: AgentRuntimeDependencyProvider,
        session_changes_service: SessionChangesRecorderProtocol,
        tool_selection_store: ToolSelectionReader,
        message_stream_store: MessageStreamStore,
        workspace_id: str,
        workspace_root: Path,
        session_catalog: SessionCatalogComponents,
        external_resource_leases: ExternalResourceLeaseLedger | None = None,
        workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
        graph_binding_store: GraphBindingStorePort,
        model_timeout_seconds: float | None = None,
        tool_timeout_seconds: float | None = None,
        residency_tracker: ThreadResidencyTracker,
        resource_activation_factory: Callable[[ResourceRegistry], ResourceActivationCoordinator],
        team_resource_subscriber: Callable[[str, str, ResourceRegistry, LifetimeScope], None],
        # Turn 控制 inbox 状态路径解析器；None 时用 SQLite catalog 解析到会话节点。
        control_inbox_state_path: Callable[[str, str], Path] | None = None,
        gateway_skill_sources: GatewaySkillSources | None = None,
    ):
        # OpenSpec 8.4：进程内复用的只有不含 Session/Thread 的 graph
        # blueprint/topology 投影——这里缓存的是 Provider 工具面定义（纯 DTO）
        # 与它的 MCP 工具面指纹；键不含 session/thread，值不捕获会话闭包，也
        # 不携带任何执行状态。真实 thread 图由 tracker 的 generation scope 持有，
        # invocation 的 admission 与工具调用 ContextVar 由 Runner 每轮绑定。
        self._tool_face_cache: dict[tuple[object, ...], list[dict[str, Any]]] = {}
        self._config_service = config_service
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._bus = job_event_bus
        self._dependency_provider = dependency_provider
        self._session_changes_service = session_changes_service
        self._tool_selection_store = tool_selection_store
        self._message_stream_store = message_stream_store
        self._workspace_id = validate_workspace_id(workspace_id)
        self._workspace_root = workspace_root
        self._session_catalog = session_catalog
        self._control_inbox_state_path = (
            control_inbox_state_path
            if control_inbox_state_path is not None
            else self._resolve_control_inbox_state_path
        )
        self._external_resource_leases = external_resource_leases
        self._workspace_file_resource_registry = workspace_file_resource_registry
        self._gateway_skill_sources = gateway_skill_sources
        self._graph_binding_store = graph_binding_store
        self._residency_tracker = residency_tracker
        self._resource_activation_factory = resource_activation_factory
        self._team_resource_subscriber = team_resource_subscriber
        self._model_timeout_seconds = model_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self._sweep_task: asyncio.Task[None] | None = None
        self.execution_scope_registry = TurnExecutionScopeRegistry()
        self._step_runner = StepRunner(
            StepExecutionPorts(
                config_service=config_service,
                job_event_bus=job_event_bus,
                tool_selection_store=tool_selection_store,
                session_changes_service=session_changes_service,
                message_stream_store=message_stream_store,
                background_message_bus=background_message_bus,
                committed_item_observer=self._acknowledge_tool_results,
                workspace_root=workspace_root,
                control_inbox_state_path=self._control_inbox_state_path,
                agent_factory=self._acquire_step_agent,
                checkpointer_provider=dependency_provider.get_checkpointer,
                session_service_provider=dependency_provider.get_session_service,
                external_resource_leases=external_resource_leases,
                model_timeout_seconds=model_timeout_seconds,
            ),
            self.execution_scope_registry,
        )

    def _acknowledge_tool_results(
        self, session_id: str, thread_id: str, items: Sequence[CanonicalItemRecord],
    ) -> None:
        self._dependency_provider.get_mcp_catalog_owner().execution_handles.acknowledge_tool_results(
            session_id, thread_id, items,
        )
        self._background_message_bus.acknowledge_tool_results(session_id, thread_id, items)

    def _resolve_control_inbox_state_path(
        self, session_id: str, turn_stream_id: str
    ) -> Path:
        """把 Turn 控制 inbox 状态落到该会话节点内。

        Turn 控制事实按 session 归属，MUST 与其它会话数据聚合在同一
        ``.boxteam/sessions/{...}/{session_id}/`` 节点下；随会话物理目录删除
        被一并回收，不散落到工作区级 ``.boxteam/control/``。
        """
        return (
            get_session_path_resolver().resolve_session_node_for_runtime(session_id)
            / "control"
            / f"{turn_stream_id}.json"
        )

    def _build_agent(
        self,
        *,
        session_id: str,
        thread_id: str,
        agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
        preferred_provider_id: str | None,
        include_team_tools: bool,
        mcp_tools: Sequence[BaseTool],
        mcp_catalog_owner: McpCatalogOwner | None = None,
        resource_registry: ResourceRegistry,
        resource_activation: ResourceActivationCoordinator | None,
        owns_thread: bool = True,
        lifetime_scope: LifetimeScope | None = None,
        skill_catalog: PublishedSkillCatalog | None = None,
    ) -> AgentEventSource:
        """一个 thread generation 的编译边界。

        session/thread 由 ``ThreadRuntimeBinding`` 显式注入，编译时不携带 invocation。

        ``owns_thread=False`` 只用于抽取工具面 blueprint 的合成构建：该构建不
        代表任何 durable thread，因此既不建立捕获会话身份的 workspace source
        reactor/CSM 订阅，也不为合成会话持久化 GraphBinding。Provider 可见工具面
        （信封 + ``skill_load`` + 内置/自定义工具）由同一工厂规则产出，与是否
        拥有 thread 无关。
        """
        return build_session_agent_runtime(
            session_id=session_id,
            thread_id=thread_id,
            agent_id=agent_id,
            tool_selection_reader=self._tool_selection_store,
            config_service=self._config_service,
            background_task_registry=self._background_task_registry,
            background_message_bus=self._background_message_bus,
            job_event_bus=self._bus,
            dependency_provider=self._dependency_provider,
            mcp_tools=mcp_tools,
            mcp_catalog_owner=mcp_catalog_owner,
            resource_registry=resource_registry,
            resource_activation=resource_activation,
            execution_overrides=execution_overrides,
            model_visibility_overrides=model_visibility_overrides,
            preferred_provider_id=preferred_provider_id,
            tool_timeout_seconds=self._tool_timeout_seconds,
            workspace_file_resource_registry=self._workspace_file_resource_registry,
            reactor_lifetime_scope=lifetime_scope if owns_thread else None,
            skill_catalog=skill_catalog,
            graph_binding_store=self._graph_binding_store,
            workspace_root=self._workspace_root,
            include_team_tools=include_team_tools,
            owns_thread=owns_thread,
        )

    async def _acquire_step_agent(
        self,
        *,
        admission: ExecutionAdmission,
        agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
        preferred_provider_id: str | None,
        include_team_tools: bool,
    ) -> ThreadRuntimeLease[StepAgentRuntime]:
        """只有真实准入可物化 runtime；代 key 与 builder 均不保留该 admission。"""
        self._check_sweeper()
        pair = MutationIntentOwner(admission.session_id, admission.thread_id)
        binding = resolve_graph_binding(
            store=self._graph_binding_store,
            owner=GraphBindingOwnerKey(pair.session_id, pair.thread_id),
        )
        snapshot = self._config_service.get_snapshot()
        # 默认选择与同一显式选择属于同一个已冻结 Provider，不应更换 thread generation。
        with self._config_service.use_snapshot(snapshot):
            provider_id = self._config_service.resolve_agent_provider_id(
                agent_id, preferred_provider_id
            )
        mcp_owner = self._dependency_provider.get_mcp_catalog_owner()
        publication = mcp_owner.publication_snapshot()
        mcp_tools = tuple(publication.tools.values())
        # Job 执行根已经绑定真实 Gateway；空 Context builder 与后台发布显式用此身份。
        gateway_id = get_current_gateway_id()
        if gateway_id is not None:
            if self._gateway_skill_sources is None:
                raise RuntimeError("正式 Gateway runtime 需要认证 Gateway Skill source owner")
            await self._gateway_skill_sources.ensure_bound(gateway_id)
        generation_key = (
            binding.selector_fields(),
            snapshot.revision,
            agent_id,
            # MCP target 的开关由 dispatch 读取，固定信封不因此重新编译。
            tuple(sorted((name, enabled) for name, enabled in execution_overrides.items() if not name.startswith("mcp__"))),
            tuple(sorted((name, visible) for name, visible in model_visibility_overrides.items() if not name.startswith("mcp__"))),
            provider_id,
            include_team_tools,
            gateway_id,
        )

        async def build(
            session_id: str, thread_id: str, scope: LifetimeScope
        ) -> StepAgentRuntime:
            file_registry = self._workspace_file_resource_registry
            if file_registry is None:
                raise RuntimeError("正式 runtime 需要 WorkspaceFileResourceRegistry")

            def compile_agent() -> tuple[StepAgentRuntime, PublishedSkillCatalog, ResourceRegistry]:
                # builder task 使用空 Context；显式恢复工作区、代配置及 thread owner。
                with (
                    workspace_context(
                        self._workspace_id,
                        self._workspace_root,
                        session_catalog=self._session_catalog,
                    ),
                    self._config_service.use_snapshot(snapshot),
                    session_thread_owner_scope(
                        MutationIntentOwner(session_id, thread_id),
                        langgraph_thread_id=thread_id,
                    ),
                ):
                    resource_registry = ResourceRegistry(
                        publication_lock=mcp_owner.publication_lock,
                        capture_validator=mcp_owner.validate_resource_capture,
                    )
                    self._team_resource_subscriber(session_id, thread_id, resource_registry, scope)
                    resource_activation = self._resource_activation_factory(resource_registry)
                    skill_catalog = build_workspace_skill_catalog(
                        self._workspace_root, registry=resource_registry,
                        file_registry=file_registry,
                        gateway_id=gateway_id,
                        gateway_sources=self._gateway_skill_sources if gateway_id is not None else None,
                    )
                    agent = self._build_agent(
                        session_id=session_id,
                        thread_id=thread_id,
                        agent_id=agent_id,
                        execution_overrides=execution_overrides,
                        model_visibility_overrides=model_visibility_overrides,
                        preferred_provider_id=provider_id,
                        include_team_tools=include_team_tools,
                        mcp_tools=mcp_tools,
                        mcp_catalog_owner=mcp_owner,
                        resource_registry=resource_registry,
                        resource_activation=resource_activation,
                        lifetime_scope=scope,
                        skill_catalog=skill_catalog,
                    )
                    return StepAgentRuntime(agent, resource_activation), skill_catalog, resource_registry

            runtime, skill_catalog, resource_registry = await asyncio.to_thread(compile_agent)
            start_workspace_skill_catalog_publication(
                skill_catalog, registry=resource_registry,
                file_registry=file_registry,
                lifetime_scope=scope,
                gateway_sources=self._gateway_skill_sources if gateway_id is not None else None,
            )
            await observe_phase(thread_id, "thread.runtime.built", details={
                "session_id": session_id, "thread_id": thread_id,
                "runtime_object_id": id(runtime), "agent_object_id": id(runtime.agent),
                "scope_object_id": id(scope),
                "graph_binding": {
                    "graph_id": binding.graph_id, "graph_revision": binding.graph_revision,
                    "graph_schema_hash": binding.graph_schema_hash,
                    "capability_profile_hash": binding.capability_profile_hash,
                },
            })
            return runtime

        return await self._residency_tracker.acquire(
            pair, admission, generation_key, build
        )

    def _check_sweeper(self) -> None:
        task = self._sweep_task
        if task is not None and task.done():
            task.result()
            raise RuntimeError("ThreadRuntime residency worker 意外停止")

    async def _sweep_idle_runtimes(self) -> None:
        try:
            await self._residency_tracker.sweep()
        except Exception:  # tracker 保留失败 owner；显式报告后按原周期重试。
            logger.exception("ThreadRuntime 闲置清理未收敛；保留失败 owner，下一周期重试")

    async def start(self) -> None:
        """lifespan 启动 idle worker，不读取或编译 cold thread。"""
        if self._sweep_task is not None:
            self._check_sweeper()
            return
        await self._sweep_idle_runtimes()

        async def sweep_periodically() -> None:
            while True:
                await asyncio.sleep(60)
                await self._sweep_idle_runtimes()

        self._sweep_task = asyncio.create_task(
            sweep_periodically(),
            name="thread-runtime-residency",
            context=Context(),
        )
        self._sweep_task.add_done_callback(
            self._residency_tracker._consume_task_exception
        )

    async def shutdown(self) -> None:
        """等待真实 lease 排空后关闭 generation scope；外部资源仍由原 owner 管理。"""
        task = self._sweep_task
        self._sweep_task = None
        errors: list[Exception] = []
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # noqa: BLE001 - worker 失败后仍排空其它真实 scope。
                errors.append(error)
        self._tool_face_cache.clear()
        try:
            await run_cleanup_shielded(self._residency_tracker.close)
        except Exception as error:  # noqa: BLE001
            errors.append(error)
        if errors:
            raise ExceptionGroup("ThreadRuntime owner 关闭失败", errors)

    async def run_step(
        self,
        session_id: str,
        message: str,
        *,
        thread_id: str,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        execution_binding: ExecutionBinding,
        resume_checkpoint_id: str | None = None,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        progress_reporter: Callable[[str], None] | None = None,
        protected_operations_settled: asyncio.Event | None = None,
    ) -> str:
        """保持 JobStepExecutor 公共接口，由独立 runner 拥有执行流程。"""
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        try:
            with workspace_context(
                self._workspace_id,
                self._workspace_root,
                session_catalog=self._session_catalog,
            ):
                return await self._step_runner.run_step(
                    session_id,
                    message,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    job_id=job_id,
                    message_id=message_id,
                    execution_binding=execution_binding,
                    resume_checkpoint_id=resume_checkpoint_id,
                    attachments=attachments,
                    message_created_at=message_created_at,
                    message_metadata=message_metadata,
                    progress_reporter=progress_reporter,
                    protected_operations_settled=protected_operations_settled,
                )
        finally:
            # Runner 已释放本轮 lease 和 invocation scope，才通知 Job 可终态/释放槽。
            if protected_operations_settled is not None:
                protected_operations_settled.set()

    async def pending_tool_checkpoint_id(self, job: JobState) -> str | None:
        """只读原 durable admission 与完整 carrier；建图和 scheduler 校验交执行 owner。"""
        checkpointer = self._dependency_provider.get_checkpointer()
        try:
            admission = await asyncio.to_thread(
                checkpointer.execution_admission_for_job,
                job.session_id, thread_id=job.thread_id, job_id=job.job_id,
            )
        except KeyError:
            return None
        binding = job.execution_binding
        if isinstance(binding, UserExecutionIdentity):
            consistent = (binding.job_id, binding.turn_id, binding.admission_idempotency_key) == (
                admission.job_id, admission.turn_id, admission.admission_idempotency_key,
            ) and admission.execution_origin == "user_turn"
        elif isinstance(binding, InternalExecutionIntent):
            consistent = (binding.execution_id, binding.admission_idempotency_key, binding.message_id) == (
                admission.execution_id, admission.admission_idempotency_key, job.message_id,
            ) and admission.execution_origin == "internal"
        else:
            raise TypeError("恢复 Job 缺少完整 execution binding")
        if not consistent:
            raise CheckpointRecoveryRejectedError(f"恢复 Job 请求与权威 execution admission 冲突: {job.job_id}")
        with session_thread_owner_scope(
            MutationIntentOwner(job.session_id, job.thread_id), langgraph_thread_id=job.thread_id,
        ):
            candidate = await persisted_pending_tool_candidate(checkpointer, admission)
        return candidate.checkpoint_id if candidate is not None else None

    def _tool_face_definitions(self, agent_id: str) -> list[dict[str, Any]]:
        """Provider 工具面定义（不含 Session/Thread 的 blueprint 投影）。

        缓存有正当的非会话价值：装配一个完整 deep agent 并按图抽取工具定义
        是昂贵构建产物，工具目录/选择查询会反复命中原样结果。缓存键不含
        session/thread，缓存值只是导出的纯 DTO；建立它时用的合成会话仅用于
        装配一次工具面，抽取出的定义不捕获该会话，也不携带执行状态。真实
        精确 thread generation 的 session/thread 依赖由 ThreadRuntimeBinding
        注入；每轮 admission 由 Runner 绑定。
        """
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")

        mcp_tools = list(self._dependency_provider.get_mcp_tools())
        # MCP 工具集是运行时可变的：用它的内容指纹参与缓存键，避免复用过期
        # 工具面。指纹复用 extension catalog 的唯一 payload 摘要实现。
        mcp_binding = seal_extension_catalog_binding_from_tools(mcp_tools)()

        config_snapshot = self._config_service.get_snapshot()
        with self._config_service.use_snapshot(config_snapshot):
            resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
            config_revision = self._config_service.get_revision()
            tool_selection = self._tool_selection_store.snapshot(resolved_agent_id)
            execution_overrides = tool_selection.execution_overrides
            model_visibility_overrides = tool_selection.model_visibility_overrides
            mode_getter = getattr(self._config_service, "get_agent_run_mode", None)
            run_mode = mode_getter() if callable(mode_getter) else None
            include_team_tools = (
                run_mode == "team" if isinstance(run_mode, str) else False
            )
            cache_key = (
                resolved_agent_id,
                config_revision,
                tuple(sorted(execution_overrides.items())),
                tuple(sorted(model_visibility_overrides.items())),
                include_team_tools,
                mcp_binding.catalog_revision,
                mcp_binding.generation,
            )
            cached = self._tool_face_cache.get(cache_key)
            if cached is not None:
                return cached

            agent = self._build_agent(
                session_id=_TOOL_INSPECTION_SESSION_ID,
                thread_id=create_prefixed_id("thr"),
                agent_id=resolved_agent_id,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                preferred_provider_id=None,
                include_team_tools=include_team_tools,
                mcp_tools=mcp_tools,
                resource_registry=ResourceRegistry(),
                resource_activation=None,
                owns_thread=False,
            )
            definitions = build_agent_tool_definitions(
                agent,
                extension_tools=mcp_tools,
            )

        # 只保留该 agent 的当前工具面：旧配置 revision / override 组合的子项
        # 不再被查询命中，留在表里只会无界增长。正在读取的调用方已持有返回值，
        # 移除旧项不影响它在途使用。
        stale_keys = [
            key
            for key in self._tool_face_cache
            if key[0] == cache_key[0] and key != cache_key
        ]
        for stale_key in stale_keys:
            del self._tool_face_cache[stale_key]
        self._tool_face_cache[cache_key] = definitions
        return definitions

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        """工具目录读写路径：返回 Provider 工具面定义（blueprint 投影）。"""
        return self._tool_face_definitions(agent_id)
