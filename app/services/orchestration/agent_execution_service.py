from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_factory import resolve_agent_id
from app.agents.graph_binding import GraphBindingStorePort
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.lifecycle import LifetimeScope
from app.core.turn_execution_scope import (
    TurnExecutionScopeRegistry,
)
from app.runtime.agent_runtime import (
    AgentRuntimeDependencyProvider,
    build_agent_tool_definitions,
    build_session_agent_runtime,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.message_stream_store import (
    MessageStreamStore,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactionRegistry,
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.orchestration.event_stream.contracts import AgentEventSource
from app.services.orchestration.execution_step.ports import StepExecutionPorts
from app.services.orchestration.execution_step.runner import StepRunner
from app.services.orchestration.thread_residency import (
    ThreadResidencyTracker,
    ThreadUnloadRequest,
)


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
        workspace_root: Path,
        external_resource_leases: ExternalResourceLeaseLedger | None = None,
        workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
        # OpenSpec 8.4：GraphBinding 持久化端口；None 时构建路径不持久化。
        graph_binding_store: GraphBindingStorePort | None = None,
        model_timeout_seconds: float | None = None,
        tool_timeout_seconds: float | None = None,
        # OpenSpec 2.8：ThreadResidency tracker；None 时不做 residency 记账。
        residency_tracker: ThreadResidencyTracker | None = None,
    ):
        # TODO(OpenSpec 8.4 后续)：该缓存复用捕获 session 闭包的已编译图，与
        # 「只复用不捕获 thread 的 graph blueprint/topology」红线有差距（R6a
        # 审计结论，行为由 test_agent_cache_rebuilds_after_config_revision_changes
        # 锁定）；blueprint/invocation 拆分留 8.4 后续轮，不在接线轮处理。
        self._agent_cache = {}
        self._config_service = config_service
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._bus = job_event_bus
        self._dependency_provider = dependency_provider
        self._session_changes_service = session_changes_service
        self._tool_selection_store = tool_selection_store
        self._message_stream_store = message_stream_store
        self._workspace_root = workspace_root
        self._external_resource_leases = external_resource_leases
        self._workspace_file_resource_registry = workspace_file_resource_registry
        self._graph_binding_store = graph_binding_store
        self._residency_tracker = residency_tracker
        self._model_timeout_seconds = model_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        # context source reaction 订阅随 agent 缓存条目释放；缓存被配置 revision
        # 淘汰时，旧 reactor 在这里关闭，不能只丢弃引用。
        self._reaction_registry_scope = LifetimeScope("agent-execution-service")
        self._reaction_registry = ContextSourceReactionRegistry(
            lifetime_scope=self._reaction_registry_scope,
        )
        self._reactor_owner_key: ContextVar[tuple[object, ...] | None] = ContextVar(
            f"boxteam_reactor_owner_key:{id(self)}",
            default=None,
        )
        # 本次 run_step 执行边界内构建产生的 step 级 owner key 收集器；
        # finally 只精确释放本次 step 的订阅，绝不按前缀误伤其它会话在途 step。
        self._step_reactor_keys: ContextVar[list[tuple[object, ...]] | None] = ContextVar(
            f"boxteam_step_reactor_keys:{id(self)}",
            default=None,
        )
        self.execution_scope_registry = TurnExecutionScopeRegistry()
        self._step_runner = StepRunner(
            StepExecutionPorts(
                config_service=config_service,
                job_event_bus=job_event_bus,
                tool_selection_store=tool_selection_store,
                session_changes_service=session_changes_service,
                message_stream_store=message_stream_store,
                workspace_root=workspace_root,
                agent_factory=self._build_step_agent,
                checkpointer_provider=dependency_provider.get_checkpointer,
                session_service_provider=dependency_provider.get_session_service,
                external_resource_leases=external_resource_leases,
                model_timeout_seconds=model_timeout_seconds,
            ),
            self.execution_scope_registry,
        )

    def _build_agent(
        self,
        *,
        session_id: str,
        agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
        preferred_provider_id: str | None,
        include_team_tools: bool,
    ) -> AgentEventSource:
        """缓存读取与真实 step 共用同一个 runtime 构建边界。"""
        return build_session_agent_runtime(
            session_id=session_id,
            agent_id=agent_id,
            config_service=self._config_service,
            background_task_registry=self._background_task_registry,
            background_message_bus=self._background_message_bus,
            job_event_bus=self._bus,
            dependency_provider=self._dependency_provider,
            execution_overrides=execution_overrides,
            model_visibility_overrides=model_visibility_overrides,
            preferred_provider_id=preferred_provider_id,
            tool_timeout_seconds=self._tool_timeout_seconds,
            workspace_file_resource_registry=self._workspace_file_resource_registry,
            reactor_lifetime_scope=self._reaction_registry_scope,
            on_reactor_created=self._record_reactor,
            graph_binding_store=self._graph_binding_store,
            workspace_root=self._workspace_root,
            include_team_tools=include_team_tools,
        )

    def _build_step_agent(
        self,
        *,
        session_id: str,
        agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
        preferred_provider_id: str | None,
        include_team_tools: bool,
    ) -> AgentEventSource:
        """StepRunner 每步直接构建 agent，不经过 _get_or_create_agent 缓存。

        该路径没有缓存 key，reactor 登记到 step 级 owner key（每次构建唯一，
        避免重试构建重复登记），并收集到本次 run_step 的收集器，step 结束后
        由 run_step 精确释放——不能按前缀释放，否则会误伤其它会话在途 step。
        """
        collected_keys = self._step_reactor_keys.get()
        if collected_keys is None:
            raise RuntimeError(
                "StepRunner 构建 agent 必须发生在 run_step 的执行边界内，"
                "否则 step 级 context source 订阅无法精确释放"
            )
        owner_key = ("step", session_id, agent_id, uuid4().hex)
        collected_keys.append(owner_key)
        token: Token[tuple[object, ...] | None] = self._reactor_owner_key.set(
            owner_key
        )
        try:
            return self._build_agent(
                session_id=session_id,
                agent_id=agent_id,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                preferred_provider_id=preferred_provider_id,
                include_team_tools=include_team_tools,
            )
        finally:
            self._reactor_owner_key.reset(token)

    def _record_reactor(
        self,
        _build_key: tuple[object, ...],
        reactor: ContextSourceReactor,
    ) -> None:
        """把本次 agent 构建产生的 reactor 登记到当前缓存 key 下。"""
        owner_key = self._reactor_owner_key.get()
        if owner_key is None:
            raise RuntimeError(
                "构建 context source reactor 时缺少 agent 缓存 owner key"
            )
        self._reaction_registry.record(owner_key, reactor)

    async def release_evicted_reactors(self) -> None:
        """释放不再属于当前 agent 缓存条目的 context source 订阅。"""
        if self._reactor_owner_key.get() is not None:
            # agent 构建失败时 owner key 可能还没被 reset；这里不猜测归属。
            raise RuntimeError("仍在构建 agent 时不能释放 context source reactor")
        active_keys = set(self._agent_cache)
        for owner_key in self._reaction_registry.active_keys:
            if owner_key in active_keys:
                continue
            if owner_key and owner_key[0] == "step":
                # step 级订阅由其所属 run_step 在结束时精确释放；其它会话
                # 的在途 step 不能被这里当作淘汰对象。
                continue
            await self._reaction_registry.release(owner_key)

    def _record_thread_residency_activity(self, session_id: str) -> None:
        """OpenSpec 2.8：runtime owner 唯一 residency 调用点。

        每次 step 开始记录该 session main thread 的活动；tracker 尚无登记代
        （backend 重启）或当前代已 idle 卸载（cold）时，先注册新一代 resident
        runtime（rehydration port），恢复该 thread 的可卸载性记账。
        """
        tracker = self._residency_tracker
        if tracker is None:
            return
        thread_id = "main"
        snapshot = tracker.snapshot(session_id, thread_id)
        if snapshot.generation == 0 or snapshot.residency == "cold":
            tracker.register_generation(session_id, thread_id)
        tracker.record_activity(session_id, thread_id)

    async def unload_thread_runtime(self, request: ThreadUnloadRequest) -> None:
        """idle unload 回调：只释放该 thread 的可重建 runtime 资源。

        generation fence：迟到 callback 必须先核验当前代，过期代 fail closed
        （直接返回，绝不释放）。只淘汰该 session 的 agent 缓存条目及其 context
        source 订阅；持久 source/tracking/prefix/ToolSet/assembly 状态逐字段
        不变。cold 后的读取路径（如 get_for_session）惰性重建全新 runtime，属
        重建而非持久状态物化，history/detail 不产生 materialize writer。
        """
        tracker = self._residency_tracker
        if tracker is None:
            raise RuntimeError("unload 回调要求 residency tracker 已装配")
        if not tracker.is_current_generation(
            request.session_id, request.thread_id, request.generation
        ):
            # 迟到 callback：该 generation 已被新一代取代，按当前 owner fail closed。
            return
        evicted_keys = [
            key for key in self._agent_cache if key[0] == request.session_id
        ]
        for key in evicted_keys:
            del self._agent_cache[key]
        await self.release_evicted_reactors()

    async def shutdown(self) -> None:
        """服务停止时释放全部 context source 订阅。"""
        self._agent_cache.clear()
        await self._reaction_registry.close()

    async def run_step(
        self,
        session_id: str,
        message: str,
        *,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        progress_reporter: Callable[[str], None] | None = None,
    ) -> str:
        """保持 JobStepExecutor 公共接口，由独立 runner 拥有执行流程。"""
        # 每次 step 开始前收敛一次订阅：上一轮因配置 revision 变化而被淘汰的
        # agent 不应继续持有来源订阅。
        await self.release_evicted_reactors()
        # OpenSpec 2.8：runtime owner 唯一 residency 调用点——记录 main thread
        # 活动，并在重启/卸载后重建时注册新一代 resident runtime。
        self._record_thread_residency_activity(session_id)
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        collected_keys: list[tuple[object, ...]] = []
        collector_token: Token[list[tuple[object, ...]] | None] = (
            self._step_reactor_keys.set(collected_keys)
        )
        try:
            return await self._step_runner.run_step(
                session_id,
                message,
                agent_id=agent_id,
                job_id=job_id,
                message_id=message_id,
                attachments=attachments,
                message_created_at=message_created_at,
                message_metadata=message_metadata,
                progress_reporter=progress_reporter,
            )
        finally:
            self._step_reactor_keys.reset(collector_token)
            # 只精确释放本次 step 构建产生的订阅；其它会话的在途 step
            # 不受影响（跨会话 run_step 可并发）。
            for owner_key in collected_keys:
                await self._reaction_registry.release(owner_key)

    def _get_or_create_agent(self, session_id: str, agent_id: str | None = None):
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")

        config_snapshot = self._config_service.get_snapshot()
        with self._config_service.use_snapshot(config_snapshot):
            resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
            config_revision = self._config_service.get_revision()
            execution_overrides = self._tool_selection_store.execution_overrides(
                resolved_agent_id
            )
            model_visibility_overrides = (
                self._tool_selection_store.model_visibility_overrides(resolved_agent_id)
            )
            mode_getter = getattr(self._config_service, "get_agent_run_mode", None)
            run_mode = mode_getter() if callable(mode_getter) else None
            include_team_tools = (
                run_mode == "team" if isinstance(run_mode, str) else False
            )
            cache_key = (
                session_id,
                resolved_agent_id,
                config_revision,
                tuple(sorted(execution_overrides.items())),
                tuple(sorted(model_visibility_overrides.items())),
            )
            if cache_key in self._agent_cache:
                return self._agent_cache[cache_key]

            # reactor 在 agent 构建期间同步产生，用 ContextVar 绑定精确的
            # 缓存 key，避免依赖构建完成后才可知的调用栈信息。
            token: Token[tuple[object, ...] | None] = self._reactor_owner_key.set(
                cache_key
            )
            try:
                agent = self._build_agent(
                    session_id=session_id,
                    agent_id=resolved_agent_id,
                    execution_overrides=execution_overrides,
                    model_visibility_overrides=model_visibility_overrides,
                    preferred_provider_id=None,
                    include_team_tools=include_team_tools,
                )
            finally:
                self._reactor_owner_key.reset(token)

        self._agent_cache[cache_key] = agent
        stale_keys = [
            key
            for key in self._agent_cache
            if key[:2] == cache_key[:2] and key != cache_key
        ]
        for stale_key in stale_keys:
            # 正在执行的 Job 已持有 Agent 局部引用；移除旧缓存不会中途改变该轮执行。
            del self._agent_cache[stale_key]
        return agent

    def _extract_final_text(self, result: dict[str, Any]) -> str:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue
            content = getattr(message, "content", None)
            if content is None:
                continue
            _, text = split_agent_content(content)
            text = text.strip()
            if text:
                return text
        raise RuntimeError(
            "Agent 执行完成但没有提取到任何最终文本。"
            f" session_id={result.get('session_id') if isinstance(result, dict) else 'unknown'}"
            " 这通常表示最终消息不是 assistant 文本，或者消息链路中出现了空响应。"
        )

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        return self._get_or_create_agent(session_id, agent_id)

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        session_id = "tools_inspection_session"
        agent = self._get_or_create_agent(session_id, agent_id)
        return build_agent_tool_definitions(
            agent,
            extension_tools=self._dependency_provider.get_mcp_tools(),
        )
