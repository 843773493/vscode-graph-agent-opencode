from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_factory import resolve_agent_id
from app.core.background_task_registry import BackgroundTaskRegistry
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
from app.services.infrastructure.message_stream_store import (
    MessageStreamStore,
)
from app.services.infrastructure.resource_manager import ResourceManager
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.orchestration.event_stream.contracts import AgentEventSource
from app.services.orchestration.execution_step.ports import StepExecutionPorts
from app.services.orchestration.execution_step.runner import StepRunner


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
        resource_manager: ResourceManager | None = None,
        model_timeout_seconds: float | None = None,
        tool_timeout_seconds: float | None = None,
    ):
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
        self._resource_manager = resource_manager
        self._model_timeout_seconds = model_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self.execution_scope_registry = TurnExecutionScopeRegistry()
        self._step_runner = StepRunner(
            StepExecutionPorts(
                config_service=config_service,
                job_event_bus=job_event_bus,
                tool_selection_store=tool_selection_store,
                session_changes_service=session_changes_service,
                message_stream_store=message_stream_store,
                workspace_root=workspace_root,
                agent_factory=self._build_agent,
                checkpointer_provider=dependency_provider.get_checkpointer,
                session_service_provider=dependency_provider.get_session_service,
                resource_manager=resource_manager,
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
            resource_manager=self._resource_manager,
            workspace_root=self._workspace_root,
            include_team_tools=include_team_tools,
        )

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
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
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

            agent = self._build_agent(
                session_id=session_id,
                agent_id=resolved_agent_id,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                preferred_provider_id=None,
                include_team_tools=include_team_tools,
            )

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
