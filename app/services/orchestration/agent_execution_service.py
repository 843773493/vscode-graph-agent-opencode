from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_factory import AGENT_GRAPH_RECURSION_LIMIT, resolve_agent_id
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.checkpoint_config import build_checkpoint_config
from app.core.identifier import create_prefixed_id
from app.core.job_context import (
    reset_active_tool_name,
    reset_current_agent_id,
    reset_current_job_id,
    reset_interruptible_phase,
    set_active_tool_name,
    set_current_agent_id,
    set_current_job_id,
    set_interruptible_phase,
)
from app.core.job_event_bus import EventType
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    ScopeCancelledError,
    TurnExecutionScopeRegistry,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.prompting import PromptSection, internal_message_factory
from app.runtime.agent_runtime import (
    AgentRuntimeDependencyProvider,
    build_agent_tool_definitions,
    build_session_agent_runtime,
    get_configured_custom_tool_names,
    get_workspace_custom_tool_skill_sources,
)
from app.schemas.event import ModelTokenUsagePayload
from app.schemas.internal_v2.message import AttachmentRef
from app.services.business.message_display import (
    DISPLAY_CONTENT_METADATA_KEY,
)
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
    persist_user_message_checkpoint,
)
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
)
from app.services.business.user_content_builder import UserContentBuilder
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.message_stream_store import (
    MessageStreamStore,
    MessageStreamTerminalError,
)
from app.services.infrastructure.resource_manager import ResourceManager
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.orchestration.agent_event_stream_processor import (
    AgentEventStreamTimeoutError,
    SuccessfulToolCall,
    last_model_token_usage,
    process_agent_event_stream,
)
from app.services.orchestration.agent_stream_helpers import (
    build_human_response_metadata,
    unwrap_json_string_tool_result,
)
from app.services.orchestration.message_stream_runtime import (
    MessageStreamRuntime,
    MessageStreamTraceObserver,
)

EMPTY_RESPONSE_RETRY_LIMIT = 2
CUSTOM_TOOL_RESPONSE_RETRY_LIMIT = 2
DELEGATED_REPORT_RETRY_LIMIT = 2
_STRUCTURED_CANCELLATION_REASONS = frozenset(
    {
        "job_startup_timeout",
        "job_timeout",
        "scope_deadline_exceeded",
        "tool_dispatch_timeout",
    }
)


def _cancelled_error_reason(error: asyncio.CancelledError) -> str | None:
    """从任务取消异常中恢复结构化 scope reason。"""
    message = str(error)
    scope_prefix = "运行时 scope 已取消: reason="
    if message.startswith(scope_prefix):
        return message.removeprefix(scope_prefix) or None
    return message or None


def _build_empty_response_retry_reminder(attempt: int) -> str:
    return (
        "上一轮模型响应没有产生任何用户可见的最终回复，也没有完成可继续展示的结果。"
        "这通常表示你只输出了内部推理。"
        f"请继续处理当前用户请求，这是第 {attempt} 次空响应恢复。"
        "如果需要调用工具，必须通过工具调用通道真实调用工具；"
        "如果已经有足够信息，请输出用户可见的最终回复。"
        "不要只复述计划、步骤或内部思考。"
    )


def _build_delegated_report_retry_reminder(
    *,
    parent_session_id: str,
    attempt: int,
    allow_progress: bool = False,
) -> str:
    progress_instruction = (
        "如果本轮收到的是下级进度，可以用 kind=progress 原样中继；"
        if allow_progress
        else ""
    )
    return (
        "这是委派子会话的首轮任务。你输出了普通最终文本，但父 Agent 不会自动收到它。"
        "必须调用 send_message_to_session 把问题、失败说明或最终结果发送给父会话。"
        f"target_session_id={parent_session_id}，simulate_user=false。"
        f"{progress_instruction}"
        f"这是第 {attempt} 次通信恢复；不要只再次输出普通最终文本。"
    )


def _has_valid_delegated_report(
    successful_tool_calls: list[SuccessfulToolCall],
    *,
    parent_session_id: str,
    allowed_kinds: frozenset[str] = frozenset({"question", "result"}),
) -> bool:
    for call in successful_tool_calls:
        if call.tool_name != "send_message_to_session":
            continue
        if call.tool_args.get("target_session_id") != parent_session_id:
            continue
        if call.tool_args.get("simulate_user", False) is not False:
            continue
        if call.tool_args.get("kind", "result") not in allowed_kinds:
            continue
        return True
    return False


def _has_valid_session_question_reply(
    successful_tool_calls: list[SuccessfulToolCall],
    *,
    sender_session_id: str,
    communication_id: str,
) -> bool:
    for call in successful_tool_calls:
        if call.tool_name != "send_message_to_session":
            continue
        if call.tool_args.get("target_session_id") != sender_session_id:
            continue
        if call.tool_args.get("simulate_user", False) is not False:
            continue
        if call.tool_args.get("kind", "result") != "reply":
            continue
        if call.tool_args.get("reply_to_communication_id") != communication_id:
            continue
        return True
    return False


def _custom_tools_requested_by_message(
    message: str,
    configured_custom_tool_names: set[str],
) -> set[str]:
    return {
        tool_name
        for tool_name in configured_custom_tool_names
        if tool_name and tool_name in message
    }


def _build_missing_custom_tool_retry_reminder(
    *,
    missing_tool_names: set[str],
    attempt: int,
) -> str:
    tools_text = "、".join(sorted(missing_tool_names))
    return (
        "上一轮模型输出了最终正文，但本轮用户请求明确要求执行以下工作区扩展工具，"
        f"而这些工具还没有完成真实工具调用：{tools_text}。"
        f"这是第 {attempt} 次扩展工具调用恢复。"
        "必须通过工具调用通道调用 invoke_custom_tool，"
        '参数格式为 {"tool_name": "<目标扩展工具名>", "arguments": {}}。'
        "不要只描述调用计划，不要把工具名称或 JSON 参数写成普通正文。"
        "工具返回后，最终回复只能包含用户需要看到的结果。"
    )


def _internal_retry_human_message(
    *,
    message_id: str,
    kind: str,
    reminder: str,
    metadata: dict[str, object],
) -> HumanMessage:
    prepared = internal_message_factory.build(
        kind=kind,
        control=reminder,
        sections=(PromptSection("control_context", metadata),),
        metadata=metadata,
    )
    return HumanMessage(
        id=message_id,
        content=prepared.content,
        response_metadata=prepared.metadata,
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
                self._tool_selection_store.model_visibility_overrides(
                    resolved_agent_id
                )
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

            agent = build_session_agent_runtime(
                session_id=session_id,
                agent_id=agent_id or resolved_agent_id,
                config_service=self._config_service,
                background_task_registry=self._background_task_registry,
                background_message_bus=self._background_message_bus,
                job_event_bus=self._bus,
                dependency_provider=self._dependency_provider,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                tool_timeout_seconds=self._tool_timeout_seconds,
                resource_manager=self._resource_manager,
                workspace_root=self._workspace_root,
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
        config_snapshot = self._config_service.get_snapshot()
        with self._config_service.use_snapshot(config_snapshot):
            return await self._run_step_with_snapshot(
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

    async def _run_step_with_snapshot(
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
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        config_snapshot = self._config_service.get_snapshot()
        with self._config_service.use_snapshot(config_snapshot):
            resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
            agent_runtime_config = self._config_service.get_agent_runtime_config(
                resolved_agent_id
            )
            require_delegated_report = agent_runtime_config.get(
                "require_delegated_report",
                False,
            )
            if not isinstance(require_delegated_report, bool):
                raise TypeError(
                    "Agent 运行时配置 require_delegated_report 必须是布尔值"
                )
            mode_getter = getattr(self._config_service, "get_agent_run_mode", None)
            run_mode = mode_getter() if callable(mode_getter) else None
            include_team_tools = (
                run_mode == "team" if isinstance(run_mode, str) else False
            )
        if self._bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 JobEventBus")
        bus = self._bus

        if not job_id:
            raise ValueError(f"run_step 缺少 job_id: session_id={session_id} agent_id={agent_id}")
        if not message_id:
            raise ValueError(f"run_step 缺少用户 message_id: session_id={session_id} job_id={job_id}")
        if not message_created_at:
            raise ValueError(
                f"run_step 缺少用户 message_created_at: session_id={session_id} job_id={job_id}"
            )
        effective_job_id = job_id
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[agent_execution_service] run_step begin: session_id=%s job_id=%s agent_id=%s message_length=%s", session_id, effective_job_id, resolved_agent_id, len(message or ""))

        # 注意：业务键（session_id / job_id）不放入 configurable —— session_id
        # 与 thread_id 重复、job_id 已经通过 set_current_job_id 维护在 contextvars。
        # 中间件通过 runtime.configurable 取不到这些键时，会回退到 contextvars
        # （见 LLMLoggingMiddleware._get_job_id 的优先级链）。
        config = {
            **build_checkpoint_config(session_id),
            "recursion_limit": AGENT_GRAPH_RECURSION_LIMIT,
        }

        job_token = set_current_job_id(effective_job_id)
        agent_token = set_current_agent_id(resolved_agent_id)
        interruptible_phase_token = set_interruptible_phase("text")
        active_tool_name_token = set_active_tool_name(None)
        SessionInterruptState.set(
            session_id,
            phase=None,
            tool_name=None,
            clear_active_tools=True,
        )

        async def _publish(event_type: str, payload: dict[str, Any]) -> None:
            await bus.publish(
                job_id=effective_job_id,
                event_type=event_type,
                payload=payload,
                agent_id=resolved_agent_id,
            )

        message_stream_writer = await self._message_stream_store.open(
            session_id=session_id,
            turn_id=effective_job_id,
            job_id=effective_job_id,
        )
        message_stream_trace_observer = MessageStreamTraceObserver(_publish)
        message_stream_runtime = MessageStreamRuntime(
            message_stream_writer,
            normalized_block_observer=message_stream_trace_observer.observe,
        )
        turn_scope = self.execution_scope_registry.create(
            message_stream_writer.turn_stream_id
        )
        control_inbox = AgentControlInbox(
            message_stream_writer.turn_stream_id,
            state_path=(
                self._workspace_root
                / ".boxteam"
                / "control"
                / f"{message_stream_writer.turn_stream_id}.json"
            ),
        )
        self.execution_scope_registry.register_inbox(
            message_stream_writer.turn_stream_id,
            control_inbox,
        )
        control_loop_stop_event = asyncio.Event()
        control_loop_task = asyncio.create_task(
            AgentLoopControlCoordinator(
                scope=turn_scope,
                inbox=control_inbox,
                writer=message_stream_writer,
            ).run(control_loop_stop_event)
        )
        if self._resource_manager is not None:
            turn_scope.register_cleanup(
                lambda: self._resource_manager.cancel_turn(
                    message_stream_writer.turn_stream_id
                )
            )
        turn_scope_token = set_current_turn_execution_scope(turn_scope)
        message_delta_token = set_current_model_delta_sink(message_stream_runtime)

        final_text = ""
        latest_model_content_blocks: tuple[dict[str, object], ...] = ()
        turn_token_usage_parts: list[ModelTokenUsagePayload] = []
        with self._config_service.use_snapshot(config_snapshot):
            configured_custom_tool_names = get_configured_custom_tool_names(
                agent_id=resolved_agent_id,
                config_service=self._config_service,
            )
            custom_tool_skill_sources = get_workspace_custom_tool_skill_sources(
                agent_id=resolved_agent_id,
                config_service=self._config_service,
            )
        execution_overrides = self._tool_selection_store.execution_overrides(
            resolved_agent_id
        )
        model_visibility_overrides = (
            self._tool_selection_store.model_visibility_overrides(resolved_agent_id)
        )
        configured_custom_tool_names = {
            tool_name
            for tool_name in configured_custom_tool_names
            if execution_overrides.get(tool_name) is not False
        }
        requested_custom_tool_names = _custom_tools_requested_by_message(
            message,
            configured_custom_tool_names,
        )
        resolved_attachments = list(attachments or [])
        resolved_message_metadata = dict(message_metadata or {})
        resolved_message_metadata.pop(DISPLAY_CONTENT_METADATA_KEY, None)
        preferred_provider_id_value = resolved_message_metadata.pop(
            "boxteam_session_provider_id",
            None,
        )
        if preferred_provider_id_value is not None and not isinstance(
            preferred_provider_id_value,
            str,
        ):
            raise TypeError("会话模型 provider id 必须是字符串")
        preferred_provider_id = preferred_provider_id_value
        human_content_result = UserContentBuilder(
            workspace_root=self._workspace_root,
        ).build(message, resolved_attachments)
        human_content = human_content_result.content
        human_response_metadata = build_human_response_metadata(
            message_id=message_id,
            display_content=None,
            attachments=resolved_attachments,
            message_created_at=message_created_at,
            message_metadata=resolved_message_metadata,
            attachment_diagnostics=human_content_result.diagnostics,
        )
        raw_message_metadata = human_response_metadata.get("message_metadata")
        if raw_message_metadata is not None and not isinstance(
            raw_message_metadata,
            dict,
        ):
            raise TypeError("HumanMessage message_metadata 必须是对象")
        human_response_metadata["message_metadata"] = {
            **(raw_message_metadata or {}),
            "turn_id": effective_job_id,
            "job_id": effective_job_id,
        }
        message_source = resolved_message_metadata.get("source")
        message_kind = resolved_message_metadata.get("kind")
        requires_delegated_report = (
            require_delegated_report
            and message_source == "session_subagent_delegation"
        )
        parent_session_id = resolved_message_metadata.get("parent_session_id")
        if (
            require_delegated_report
            and message_source == "send_message_to_session"
            and message_kind in {"reply", "progress", "result"}
        ):
            session_service = self._dependency_provider.get_session_service()
            current_session = await session_service.get(session_id)
            if current_session.delegation is not None:
                requires_delegated_report = True
                parent_session_id = (
                    current_session.delegation.parent_session_id
                )
        if requires_delegated_report and not isinstance(parent_session_id, str):
            raise RuntimeError(
                "委派子会话首轮缺少 parent_session_id 元数据: "
                f"session_id={session_id} job_id={effective_job_id}"
            )
        delegated_report_allowed_kinds = (
            frozenset({"question", "progress", "result"})
            if message_source == "send_message_to_session"
            and message_kind == "progress"
            else frozenset({"question", "result"})
        )
        requires_session_question_reply = (
            resolved_message_metadata.get("source") == "send_message_to_session"
            and resolved_message_metadata.get("kind") == "question"
            and resolved_message_metadata.get("reply_required") is True
        )
        question_sender_session_id = resolved_message_metadata.get(
            "sender_session_id"
        )
        question_communication_id = resolved_message_metadata.get(
            "communication_id"
        )
        if requires_session_question_reply and (
            not isinstance(question_sender_session_id, str)
            or not isinstance(question_communication_id, str)
        ):
            raise RuntimeError(
                "跨会话问题缺少可信回复路由元数据: "
                f"session_id={session_id} job_id={effective_job_id}"
            )

        try:
            if progress_reporter is not None:
                progress_reporter("agent_start")
            await _publish(EventType.AGENT_START, {
                "message": "agent 启动，准备处理用户请求",
                "agent_id": resolved_agent_id,
            })

            logger.info(
                "[agent_execution_service] agent runtime build begin: job_id=%s",
                effective_job_id,
            )

            with self._config_service.use_snapshot(config_snapshot):
                agent = await asyncio.to_thread(
                    build_session_agent_runtime,
                    session_id=session_id,
                    agent_id=resolved_agent_id,
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

            logger.info(
                "[agent_execution_service] agent runtime ready: job_id=%s",
                effective_job_id,
            )
            if progress_reporter is not None:
                progress_reporter("agent_runtime_ready")

            next_input_messages = [
                HumanMessage(
                    id=message_id,
                    content=human_content,
                    response_metadata=human_response_metadata,
                )
            ]
            checkpointer = getattr(
                self._dependency_provider,
                "get_checkpointer",
                lambda: None,
            )()
            if checkpointer is not None:
                await asyncio.to_thread(
                    persist_user_message_checkpoint,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    message=next_input_messages[0],
                )
            logger.info(
                "[agent_execution_service] agent loop ready: job_id=%s",
                effective_job_id,
            )
            if progress_reporter is not None:
                progress_reporter("agent_loop_ready")
            empty_response_retries = 0
            custom_tool_response_retries = 0
            delegated_report_retries = 0
            successful_tool_calls: list[SuccessfulToolCall] = []
            completed_custom_tool_names: set[str] = set()
            while True:
                stream_result = await process_agent_event_stream(
                    agent=agent,
                    input_payload={"messages": next_input_messages},
                    config=config,
                    session_id=session_id,
                    turn_id=effective_job_id,
                    agent_id=resolved_agent_id,
                    custom_tool_skill_sources=custom_tool_skill_sources,
                    publish=_publish,
                    session_changes_service=self._session_changes_service,
                    workspace_root=self._workspace_root,
                    message_stream_runtime=message_stream_runtime,
                    cancellation_signal=turn_scope.cancellation_signal,
                    execution_scope=turn_scope,
                    model_timeout_seconds=self._model_timeout_seconds,
                    progress_reporter=progress_reporter,
                )
                turn_token_usage_parts.append(stream_result.token_usage)
                final_text = stream_result.final_text
                successful_tool_calls.extend(stream_result.successful_tool_calls)
                completed_custom_tool_names.update(
                    stream_result.completed_custom_tool_names
                )
                final_text = unwrap_json_string_tool_result(
                    final_text,
                    stream_result.last_tool_result_text,
                )
                normalized_final_text = message_stream_runtime.normalized_final_text()
                if (
                    normalized_final_text.strip()
                    and stream_result.final_text.strip()
                    and normalized_final_text.strip() != stream_result.final_text.strip()
                ):
                    logger.warning(
                        "消息流规范化文本与 AgentLoop 最终聚合文本不一致: "
                        "job_id=%s model_call_id=%s normalized_length=%s "
                        "aggregated_length=%s",
                        effective_job_id,
                        message_stream_runtime.current_model_call_id,
                        len(normalized_final_text),
                        len(stream_result.final_text),
                    )
                latest_model_content_blocks = stream_result.latest_model_content_blocks
                missing_custom_tool_names = (
                    requested_custom_tool_names - completed_custom_tool_names
                )
                missing_delegated_report = (
                    requires_delegated_report
                    and not _has_valid_delegated_report(
                        successful_tool_calls,
                        parent_session_id=parent_session_id,
                        allowed_kinds=delegated_report_allowed_kinds,
                    )
                )
                missing_session_question_reply = (
                    requires_session_question_reply
                    and not _has_valid_session_question_reply(
                        successful_tool_calls,
                        sender_session_id=question_sender_session_id,
                        communication_id=question_communication_id,
                    )
                )
                validation_succeeded = bool(
                    final_text
                    and not missing_custom_tool_names
                    and not missing_delegated_report
                    and not missing_session_question_reply
                )
                await message_stream_runtime.complete_model(
                    outcome=(
                        "accepted" if validation_succeeded else "validation_failed"
                    ),
                    reason=(
                        None
                        if validation_succeeded
                        else "AgentLoop 最终业务校验未通过"
                    ),
                )
                if (
                    validation_succeeded
                ):
                    break
                await message_stream_runtime.retrying("AgentLoop 最终业务校验未通过")
                if final_text and missing_custom_tool_names:
                    custom_tool_response_retries += 1
                    if custom_tool_response_retries > CUSTOM_TOOL_RESPONSE_RETRY_LIMIT:
                        raise RuntimeError(
                            "Agent 返回了最终文本，但没有执行用户请求中的自定义扩展工具。"
                            f" session_id={session_id} job_id={effective_job_id} "
                            f"missing_tools={sorted(missing_custom_tool_names)} "
                            f"retry_limit={CUSTOM_TOOL_RESPONSE_RETRY_LIMIT}"
                        )

                    reminder = _build_missing_custom_tool_retry_reminder(
                        missing_tool_names=missing_custom_tool_names,
                        attempt=custom_tool_response_retries,
                    )
                    logger.warning(
                        "[agent_execution_service] custom tool requested but not executed, retrying: "
                        "job_id=%s missing_tools=%s attempt=%s",
                        effective_job_id,
                        sorted(missing_custom_tool_names),
                        custom_tool_response_retries,
                    )
                    await _publish(
                        EventType.AGENT_START,
                        {
                            "message": "模型没有执行用户请求中的扩展工具，继续请求真实工具调用",
                            "agent_id": resolved_agent_id,
                        },
                    )
                    next_input_messages = [
                        _internal_retry_human_message(
                            message_id=f"{effective_job_id}:missing_custom_tool_retry:{custom_tool_response_retries}",
                            kind="missing_custom_tool_retry",
                            reminder=reminder,
                            metadata={
                                "source": "missing_custom_tool_retry",
                                "attempt": custom_tool_response_retries,
                                "missing_tools": sorted(missing_custom_tool_names),
                            },
                        )
                    ]
                    continue

                if final_text and missing_delegated_report:
                    delegated_report_retries += 1
                    if delegated_report_retries > DELEGATED_REPORT_RETRY_LIMIT:
                        raise RuntimeError(
                            "委派子 Agent 返回了普通最终文本，但没有通过 "
                            "send_message_to_session 向父会话报告。"
                            f" session_id={session_id} job_id={effective_job_id} "
                            f"parent_session_id={parent_session_id} "
                            f"retry_limit={DELEGATED_REPORT_RETRY_LIMIT}"
                        )
                    reminder = _build_delegated_report_retry_reminder(
                        parent_session_id=parent_session_id,
                        attempt=delegated_report_retries,
                        allow_progress="progress"
                        in delegated_report_allowed_kinds,
                    )
                    await _publish(
                        EventType.AGENT_START,
                        {
                            "message": "委派子 Agent 未通过会话工具报告，继续请求真实工具调用",
                            "agent_id": resolved_agent_id,
                        },
                    )
                    next_input_messages = [
                        _internal_retry_human_message(
                            message_id=(
                                f"{effective_job_id}:delegated_report_retry:"
                                f"{delegated_report_retries}"
                            ),
                            kind="delegated_report_retry",
                            reminder=reminder,
                            metadata={
                                "source": "delegated_report_retry",
                                "attempt": delegated_report_retries,
                                "parent_session_id": parent_session_id,
                            },
                        )
                    ]
                    continue

                if final_text and missing_session_question_reply:
                    delegated_report_retries += 1
                    if delegated_report_retries > DELEGATED_REPORT_RETRY_LIMIT:
                        raise RuntimeError(
                            "Agent 收到跨会话问题后返回了普通文本，但没有通过 "
                            "send_message_to_session 定向回复。"
                            f" session_id={session_id} job_id={effective_job_id} "
                            f"sender_session_id={question_sender_session_id} "
                            f"communication_id={question_communication_id} "
                            f"retry_limit={DELEGATED_REPORT_RETRY_LIMIT}"
                        )
                    reminder = (
                        "你正在回答另一个 Agent 的跨会话问题，普通最终文本不会送达提问方。"
                        "必须调用 send_message_to_session："
                        f"target_session_id={question_sender_session_id}，"
                        "simulate_user=false，kind=reply，"
                        f"reply_to_communication_id={question_communication_id}。"
                        f"这是第 {delegated_report_retries} 次通信恢复。"
                    )
                    await _publish(
                        EventType.AGENT_START,
                        {
                            "message": "跨会话问题未通过会话工具回复，继续请求真实工具调用",
                            "agent_id": resolved_agent_id,
                        },
                    )
                    next_input_messages = [
                        _internal_retry_human_message(
                            message_id=(
                                f"{effective_job_id}:session_question_reply_retry:"
                                f"{delegated_report_retries}"
                            ),
                            kind="session_question_reply_retry",
                            reminder=reminder,
                            metadata={
                                "source": "session_question_reply_retry",
                                "attempt": delegated_report_retries,
                                "sender_session_id": question_sender_session_id,
                                "communication_id": question_communication_id,
                            },
                        )
                    ]
                    continue

                empty_response_retries += 1
                if empty_response_retries > EMPTY_RESPONSE_RETRY_LIMIT:
                    raise RuntimeError(
                        "Agent 连续返回空的用户可见回复。"
                        f" session_id={session_id} job_id={effective_job_id} "
                        f"retry_limit={EMPTY_RESPONSE_RETRY_LIMIT}"
                    )

                reminder = _build_empty_response_retry_reminder(empty_response_retries)
                logger.warning(
                    "[agent_execution_service] empty visible response, retrying: "
                    "job_id=%s attempt=%s",
                    effective_job_id,
                    empty_response_retries,
                )
                await _publish(
                    EventType.AGENT_START,
                    {
                        "message": "模型只返回了内部推理，继续请求工具调用或最终回复",
                        "agent_id": resolved_agent_id,
                    },
                )
                next_input_messages = [
                    _internal_retry_human_message(
                        message_id=f"{effective_job_id}:empty_response_retry:{empty_response_retries}",
                        kind="empty_response_retry",
                        reminder=reminder,
                        metadata={
                            "source": "empty_response_retry",
                            "attempt": empty_response_retries,
                        },
                    )
                ]

            if final_text:
                SessionInterruptState.set(
                    session_id,
                    phase=None,
                    tool_name=None,
                    clear_active_tools=True,
                )
                set_interruptible_phase("text")
                set_active_tool_name(None)
            turn_token_usage = last_model_token_usage(turn_token_usage_parts)
            if checkpointer is not None:
                assistant_message_id = create_prefixed_id("msg")
                assistant_message_created_at = datetime.now(UTC)
                persisted = await asyncio.to_thread(
                    persist_standard_assistant_checkpoint,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    turn_id=effective_job_id,
                    content_blocks=latest_model_content_blocks,
                    final_text=final_text,
                    message_id=assistant_message_id,
                    message_created_at=assistant_message_created_at,
                    token_usage=turn_token_usage,
                )
                if final_text and not persisted:
                    raise RuntimeError(
                        "最终 assistant 消息未能写入 checkpoint: "
                        f"session_id={session_id} job_id={effective_job_id}"
                    )

            try:
                await message_stream_writer.close_completed()
            except MessageStreamTerminalError:
                # 中断请求可能在最终业务校验与 stream.completed 之间线性化。
                # 此时执行已经停止，必须确认中断事实，不能把用户请求覆盖为完成。
                stream_state = await self._message_stream_store.get_state(
                    message_stream_writer.turn_stream_id
                )
                interrupt_state = stream_state.get("interrupt_state")
                interrupt_request_id = (
                    interrupt_state.get("request_id")
                    if isinstance(interrupt_state, dict)
                    and interrupt_state.get("status") == "requested"
                    else None
                )
                if stream_state.get("stream_status") != "interrupting" or not isinstance(
                    interrupt_request_id, str
                ):
                    raise
                await message_stream_writer.close_interrupted(interrupt_request_id)

            await _publish(EventType.AGENT_END, {
                "final_text": final_text,
                "agent_id": resolved_agent_id,
                "token_usage": turn_token_usage.model_dump(mode="json"),
            })

            logger.info("[agent_execution_service] response ready: job_id=%s response_length=%s", effective_job_id, len(final_text))
            return final_text

        except asyncio.CancelledError as cancellation_error:
            state = SessionInterruptState.get(session_id)
            user_interrupt = state.interrupt_request_id is not None
            cancellation_reason = _cancelled_error_reason(cancellation_error)
            pending_tool_calls = message_stream_runtime.pending_tool_calls()
            complete_pending_tool_call_ids = [
                tool_call_id
                for tool_call_id, _tool_name, arguments_complete in pending_tool_calls
                if arguments_complete
            ]
            failure_code = (
                "user_interrupt"
                if user_interrupt
                else "job_startup_timeout"
                if cancellation_reason == "job_startup_timeout"
                else "job_timeout"
                if cancellation_reason == "job_timeout"
                else "scope_deadline_exceeded"
                if cancellation_reason == "scope_deadline_exceeded"
                else "tool_dispatch_timeout"
                if complete_pending_tool_call_ids
                else "execution_lost"
            )
            failure_message = (
                "用户中断后的 AgentLoop 失败"
                if user_interrupt
                else "Job 启动超过等待首个模型/工具事件的上限"
                if failure_code == "job_startup_timeout"
                else "Job 执行超过总超时上限"
                if failure_code == "job_timeout"
                else (
                    "模型工具调用参数已完整，但工具执行分派在取消前没有启动: "
                    f"tool_calls={complete_pending_tool_call_ids}"
                )
                if failure_code == "tool_dispatch_timeout"
                else "AgentLoop 因内部取消而结束，未收到用户中断请求"
            )
            if not (user_interrupt and state.user_interrupt_reminder_injected):
                try:
                    await asyncio.to_thread(
                        persist_interrupt_checkpoint,
                        checkpointer=getattr(
                            self._dependency_provider,
                            "get_checkpointer",
                            lambda: None,
                        )(),
                        session_id=session_id,
                        current_text=state.current_text,
                        active_tool_name=state.tool_name,
                        checkpoint_source=(
                            "interrupt" if user_interrupt else failure_code
                        ),
                    )
                    logger.info(
                        "[agent_execution_service] cancellation checkpoint persisted: "
                        "job_id=%s code=%s",
                        effective_job_id,
                        failure_code,
                    )
                except Exception:
                    # checkpoint 失败不能掩盖取消事实；消息流终态仍必须提交。
                    logger.exception(
                        "[agent_execution_service] cancellation checkpoint persistence failed: job_id=%s",
                        effective_job_id,
                    )
            if user_interrupt:
                if state.user_interrupt_reminder_injected:
                    logger.info(
                        "[agent_execution_service] job cancelled after user interrupt reminder persisted: job_id=%s",
                        effective_job_id,
                    )
                await message_stream_runtime.finalize_interruption_facts()
                await message_stream_writer.close_interrupted(state.interrupt_request_id)
            else:
                await message_stream_runtime.fail_pending_tool_calls(
                    completion_reason=failure_code,
                    error=failure_message,
                )
                await message_stream_writer.close_failed(
                    code=failure_code,
                    message=failure_message,
                    resumable=False,
                )
            raise
        except Exception as e:
            interrupt_state = SessionInterruptState.get(session_id)
            if interrupt_state.interrupt_request_id is not None:
                await message_stream_runtime.finalize_interruption_facts()
            else:
                failure_code = (
                    e.code
                    if isinstance(e, AgentEventStreamTimeoutError)
                    else e.reason
                    if isinstance(e, ScopeCancelledError)
                    and e.reason in _STRUCTURED_CANCELLATION_REASONS
                    else "execution_lost"
                    if isinstance(e, ScopeCancelledError)
                    else "execution_error"
                )
                failure_message = str(e)
                if isinstance(e, ScopeCancelledError):
                    try:
                        await asyncio.to_thread(
                            persist_interrupt_checkpoint,
                            checkpointer=getattr(
                                self._dependency_provider,
                                "get_checkpointer",
                                lambda: None,
                            )(),
                            session_id=session_id,
                            current_text=interrupt_state.current_text,
                            active_tool_name=interrupt_state.tool_name,
                            checkpoint_source=failure_code,
                        )
                    except Exception:
                        # 失败 checkpoint 不能覆盖 scope 终态；消息流仍须保留
                        # 可诊断的 failure 原因。
                        logger.exception(
                            "[agent_execution_service] scope failure checkpoint persistence failed: job_id=%s",
                            effective_job_id,
                        )
                await message_stream_runtime.fail_pending_tool_calls(
                    completion_reason=failure_code,
                    error=failure_message,
                )
                await message_stream_runtime.fail_model(
                    code=failure_code,
                    message=failure_message,
                )
            await message_stream_writer.close_failed(
                code=(
                    "user_interrupt"
                    if interrupt_state.interrupt_request_id is not None else failure_code
                ),
                message=(
                    "用户中断后的 AgentLoop 失败"
                    if interrupt_state.interrupt_request_id is not None
                    else failure_message
                ),
                after_interrupt_requested=bool(
                    interrupt_state.cancellation_reason
                ),
                resumable=False,
            )
            await _publish(EventType.ERROR, {"error": str(e), "phase": "agent_execution"})
            logger.exception(
                "[agent_execution_service] ERROR published: job_id=%s",
                effective_job_id,
            )
            raise
        finally:
            control_loop_stop_event.set()
            if not control_loop_task.done():
                control_loop_task.cancel()
            await asyncio.gather(control_loop_task, return_exceptions=True)
            reset_current_model_delta_sink(message_delta_token)
            reset_current_turn_execution_scope(turn_scope_token)
            await self.execution_scope_registry.close(
                message_stream_writer.turn_stream_id
            )
            reset_current_job_id(job_token)
            reset_current_agent_id(agent_token)
            reset_interruptible_phase(interruptible_phase_token)
            reset_active_tool_name(active_tool_name_token)
            SessionInterruptState.clear(session_id)

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        return self._get_or_create_agent(session_id, agent_id)

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        session_id = "tools_inspection_session"
        agent = self._get_or_create_agent(session_id, agent_id)
        return build_agent_tool_definitions(
            agent,
            extension_tools=self._dependency_provider.get_mcp_tools(),
        )
