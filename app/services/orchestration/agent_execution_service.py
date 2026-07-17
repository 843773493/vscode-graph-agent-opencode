from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from langchain_core.messages import AIMessage, HumanMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.job_context import (
    reset_current_agent_id,
    reset_current_job_id,
    reset_active_tool_name,
    reset_interruptible_phase,
    set_active_tool_name,
    set_current_agent_id,
    set_current_job_id,
    set_interruptible_phase,
)
from app.core.job_event_bus import EventType
from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.message import AttachmentRef
from app.schemas.event import ModelTokenUsagePayload
from app.agents.agent_factory import AGENT_GRAPH_RECURSION_LIMIT, resolve_agent_id
from app.services.infrastructure.attachment_content_service import build_human_content
from app.services.infrastructure.config_service import ConfigService
from app.abstractions.job_step_executor import JobStepExecutor
from app.runtime.agent_runtime import (
    AgentRuntimeDependencyProvider,
    build_agent_tool_definitions,
    build_session_agent_runtime,
    get_configured_custom_tool_names,
    get_workspace_custom_tool_skill_sources,
)
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
)
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
)
from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.services.orchestration.agent_event_stream_processor import (
    SuccessfulToolCall,
    combine_model_token_usage,
    process_agent_event_stream,
)
from app.services.orchestration.agent_stream_helpers import (
    build_human_response_metadata,
    unwrap_json_string_tool_result,
)


EMPTY_RESPONSE_RETRY_LIMIT = 2
CUSTOM_TOOL_RESPONSE_RETRY_LIMIT = 2
DELEGATED_REPORT_RETRY_LIMIT = 2


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
        workspace_root: Path,
    ):
        self._agent_cache = {}
        self._config_service = config_service
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._bus = job_event_bus
        self._dependency_provider = dependency_provider
        self._session_changes_service = session_changes_service
        self._tool_selection_store = tool_selection_store
        self._workspace_root = workspace_root

    def _get_or_create_agent(self, session_id: str, agent_id: str | None = None):
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")

        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        cache_key = f"{session_id}::{resolved_agent_id}"
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
        )

        self._agent_cache[cache_key] = agent
        return agent

    def _extract_final_text(self, result: Dict[str, Any]) -> str:
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
        message_role: MessageRole = MessageRole.user,
        message_metadata: dict[str, object] | None = None,
    ) -> str:
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
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

        final_text = ""
        latest_model_content_blocks: tuple[dict[str, object], ...] = ()
        turn_token_usage_parts: list[ModelTokenUsagePayload] = []
        configured_custom_tool_names = get_configured_custom_tool_names(
            agent_id=resolved_agent_id,
            config_service=self._config_service,
        )
        disabled_tool_names = self._tool_selection_store.disabled_tools(
            resolved_agent_id
        )
        configured_custom_tool_names -= disabled_tool_names
        requested_custom_tool_names = _custom_tools_requested_by_message(
            message,
            configured_custom_tool_names,
        )
        custom_tool_skill_sources = get_workspace_custom_tool_skill_sources(
            agent_id=resolved_agent_id,
            config_service=self._config_service,
        )
        resolved_attachments = list(attachments or [])
        resolved_message_metadata = dict(message_metadata or {})
        human_content = build_human_content(message, resolved_attachments)
        human_response_metadata = build_human_response_metadata(
            message_id=message_id,
            display_content=message,
            attachments=resolved_attachments,
            message_created_at=message_created_at,
            message_role=message_role,
            message_metadata=resolved_message_metadata,
        )
        message_source = resolved_message_metadata.get("source")
        message_kind = resolved_message_metadata.get("kind")
        requires_delegated_report = message_source == "session_subagent_delegation"
        parent_session_id = resolved_message_metadata.get("parent_session_id")
        if (
            message_source == "send_message_to_session"
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
            await _publish(EventType.AGENT_START, {
                "message": "agent 启动，准备处理用户请求",
                "agent_id": resolved_agent_id,
            })

            logger.info("[agent_execution_service] agent.astream_events begin: job_id=%s", effective_job_id)

            agent = build_session_agent_runtime(
                session_id=session_id,
                agent_id=resolved_agent_id,
                config_service=self._config_service,
                background_task_registry=self._background_task_registry,
                background_message_bus=self._background_message_bus,
                job_event_bus=self._bus,
                dependency_provider=self._dependency_provider,
                tool_denylist=disabled_tool_names,
            )

            next_input_messages = [
                HumanMessage(
                    id=message_id,
                    content=human_content,
                    response_metadata=human_response_metadata,
                )
            ]
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
                if final_text:
                    final_text_part_id = stream_result.final_text_part_id
                    if final_text_part_id is None:
                        raise RuntimeError(
                            "模型返回了最终文本，但模型流没有提供 markdown part_id"
                        )
                    await _publish(
                        EventType.TEXT_END,
                        {
                            "part_id": final_text_part_id,
                            "kind": "markdown",
                            "text": final_text,
                        },
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
                if (
                    final_text
                    and not missing_custom_tool_names
                    and not missing_delegated_report
                    and not missing_session_question_reply
                ):
                    break
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
                        HumanMessage(
                            id=f"{effective_job_id}:missing_custom_tool_retry:{custom_tool_response_retries}",
                            content=f"<system_reminder>\n{reminder}\n</system_reminder>",
                            response_metadata={
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
                        HumanMessage(
                            id=(
                                f"{effective_job_id}:delegated_report_retry:"
                                f"{delegated_report_retries}"
                            ),
                            content=f"<system_reminder>\n{reminder}\n</system_reminder>",
                            response_metadata={
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
                        HumanMessage(
                            id=(
                                f"{effective_job_id}:session_question_reply_retry:"
                                f"{delegated_report_retries}"
                            ),
                            content=f"<system_reminder>\n{reminder}\n</system_reminder>",
                            response_metadata={
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
                    HumanMessage(
                        id=f"{effective_job_id}:empty_response_retry:{empty_response_retries}",
                        content=f"<system_reminder>\n{reminder}\n</system_reminder>",
                        response_metadata={
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
            checkpointer = getattr(self._dependency_provider, "get_checkpointer", lambda: None)()
            turn_token_usage = combine_model_token_usage(turn_token_usage_parts)
            if checkpointer is not None:
                assistant_message_id = create_prefixed_id("msg")
                assistant_message_created_at = datetime.now(timezone.utc)
                persisted = persist_standard_assistant_checkpoint(
                    checkpointer=checkpointer,
                    session_id=session_id,
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

            await _publish(EventType.AGENT_END, {
                "final_text": final_text,
                "agent_id": resolved_agent_id,
                "token_usage": turn_token_usage.model_dump(mode="json"),
            })

            logger.info("[agent_execution_service] response ready: job_id=%s response_length=%s", effective_job_id, len(final_text))
            return final_text

        except asyncio.CancelledError:
            state = SessionInterruptState.get(session_id)
            if state.user_interrupt_reminder_injected:
                logger.info(
                    "[agent_execution_service] job cancelled after user interrupt reminder persisted: job_id=%s",
                    effective_job_id,
                )
            else:
                persist_interrupt_checkpoint(
                    checkpointer=getattr(self._dependency_provider, "get_checkpointer", lambda: None)(),
                    session_id=session_id,
                    current_text=state.current_text,
                    active_tool_name=state.tool_name,
                )
                logger.info("[agent_execution_service] job cancelled and checkpoint persisted: job_id=%s", effective_job_id)
            raise
        except Exception as e:
            await _publish(EventType.ERROR, {"error": str(e), "phase": "agent_execution"})
            logger.exception("[agent_execution_service] ERROR published: job_id=%s error=%s", effective_job_id, str(e))
            raise
        finally:
            reset_current_job_id(job_token)
            reset_current_agent_id(agent_token)
            reset_interruptible_phase(interruptible_phase_token)
            reset_active_tool_name(active_tool_name_token)
            SessionInterruptState.clear(session_id)

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        return self._get_or_create_agent(session_id, agent_id)

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        session_id = "tools_inspection_session"
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        agent = self._get_or_create_agent(session_id, resolved_agent_id)
        return build_agent_tool_definitions(agent)
