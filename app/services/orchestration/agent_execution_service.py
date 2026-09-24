from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from langchain_core.messages import AIMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_factory import resolve_agent_id
from app.agents.graph_binding import GraphBindingStorePort
from app.agents.tools.custom_invocation import (
    seal_extension_catalog_binding_from_tools,
)
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

# 工具面 blueprint 检查用的合成会话：只用于装配一次完整工具面并抽取定义，
# 不进入任何业务会话账目。抽出的定义不捕获该会话，仅用于 Provider 工具目录。
_TOOL_INSPECTION_SESSION_ID: Final[str] = "tools_inspection_session"


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
        # OpenSpec 8.4：进程内复用的只有不含 Session/Thread 的 graph
        # blueprint/topology 投影——这里缓存的是 Provider 工具面定义（纯 DTO）
        # 与它的 MCP 工具面指纹；键不含 session/thread，值不捕获会话闭包，也
        # 不携带任何执行状态。真实 invocation 的 session/thread 依赖由每次构建
        # 经 ThreadRuntimeBinding 注入。旧的「按 (session_id, agent, revision,
        # overrides) 复用整个已编译 agent」的捕获式缓存已物理删除。
        self._tool_face_cache: dict[tuple[object, ...], list[dict[str, Any]]] = {}
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
        owns_thread: bool = True,
    ) -> AgentEventSource:
        """一次 invocation 的唯一 runtime 构建边界。

        OpenSpec 8.4：session/thread 依赖只来自本次构建经
        ``build_session_agent_runtime`` 注入的 ``ThreadRuntimeBinding``；本服务
        不跨 invocation 复用任何捕获会话闭包的对象。

        ``owns_thread=False`` 只用于抽取工具面 blueprint 的合成构建：该构建不
        代表任何 durable thread，因此既不建立捕获会话身份的 workspace source
        reactor/CSM 订阅，也不为合成会话持久化 GraphBinding。Provider 可见工具面
        （信封 + ``skill_load`` + 内置/自定义工具）由同一工厂规则产出，与是否
        拥有 thread 无关。
        """
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
            workspace_file_resource_registry=(
                self._workspace_file_resource_registry if owns_thread else None
            ),
            reactor_lifetime_scope=(
                self._reaction_registry_scope if owns_thread else None
            ),
            on_reactor_created=(
                self._record_reactor if owns_thread else None
            ),
            graph_binding_store=self._graph_binding_store if owns_thread else None,
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
        """StepRunner 每步直接构建一个全新 agent（唯一 invocation 边界）。

        OpenSpec 8.4：真实执行路径没有跨 invocation 的会话复用，工具面
        blueprint 缓存只服务于工具目录查询。reactor 登记到 step 级 owner key
        （每次构建唯一，避免重试构建重复登记），并收集到本次 run_step 的收集
        器，step 结束后由 run_step 精确释放——不能按前缀释放，否则会误伤其它
        会话在途 step。
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
        """把本次 agent 构建产生的 reactor 登记到当前 step 级 owner key 下。"""
        owner_key = self._reactor_owner_key.get()
        if owner_key is None:
            raise RuntimeError(
                "构建 context source reactor 时缺少 step 级 owner key"
            )
        self._reaction_registry.record(owner_key, reactor)

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
        """idle unload 回调：核验 generation fence 后收敛该 thread 的可重建资源。

        generation fence：迟到 callback 必须先核验当前代，过期代 fail closed
        （直接返回，绝不释放）。

        OpenSpec 8.4：runtime 构建已改为按 invocation 进行，agent 与其 context
        source reactor 都是 step 级、在该 step 结束时精确释放，本服务不再按
        session 复用任何捕获会话闭包的对象。因此本回调没有 per-invocation
        会话缓存可淘汰；持久 source/tracking/prefix/ToolSet/assembly 状态与
        工具面 blueprint 缓存逐字段不变，cold 后由下一次 invocation 全新构建。
        """
        tracker = self._residency_tracker
        if tracker is None:
            raise RuntimeError("unload 回调要求 residency tracker 已装配")
        if not tracker.is_current_generation(
            request.session_id, request.thread_id, request.generation
        ):
            # 迟到 callback：该 generation 已被新一代取代，按当前 owner fail closed。
            return

    async def shutdown(self) -> None:
        """服务停止时释放全部 context source 订阅。"""
        self._tool_face_cache.clear()
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

    def _tool_face_definitions(self, agent_id: str) -> list[dict[str, Any]]:
        """Provider 工具面定义（不含 Session/Thread 的 blueprint 投影）。

        缓存有正当的非会话价值：装配一个完整 deep agent 并按图抽取工具定义
        是昂贵构建产物，工具目录/选择查询会反复命中原样结果。缓存键不含
        session/thread，缓存值只是导出的纯 DTO；建立它时用的合成会话仅用于
        装配一次工具面，抽取出的定义不捕获该会话，也不携带执行状态。真实
        invocation 的 session/thread 依赖由每次构建经 ThreadRuntimeBinding
        注入。
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
                agent_id=resolved_agent_id,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                preferred_provider_id=None,
                include_team_tools=include_team_tools,
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

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        """工具目录读写路径：返回 Provider 工具面定义（blueprint 投影）。"""
        return self._tool_face_definitions(agent_id)
