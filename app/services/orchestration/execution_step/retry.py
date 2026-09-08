"""Agent step 的 runtime build 与 retry/reminder orchestration owner。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.abstractions.session_changes import SessionChangesRecorderProtocol
from app.core.job_event_bus import EventType
from app.services.orchestration.agent_stream_helpers import (
    unwrap_json_string_tool_result,
)
from app.services.orchestration.event_stream.contracts import (
    AgentEventStreamResult,
)
from app.services.orchestration.event_stream.processor import (
    process_agent_event_stream,
)
from app.services.orchestration.execution_step.ports import StepAgentFactory
from app.services.orchestration.execution_step.reminders import (
    build_delegated_report_retry_reminder as _build_delegated_report_retry_reminder,
)
from app.services.orchestration.execution_step.reminders import (
    build_empty_response_retry_reminder as _build_empty_response_retry_reminder,
)
from app.services.orchestration.execution_step.reminders import (
    build_missing_custom_tool_retry_reminder as _build_missing_custom_tool_retry_reminder,
)
from app.services.orchestration.execution_step.reminders import (
    has_valid_delegated_report as _has_valid_delegated_report,
)
from app.services.orchestration.execution_step.reminders import (
    has_valid_session_question_reply as _has_valid_session_question_reply,
)
from app.services.orchestration.execution_step.reminders import (
    internal_retry_human_message as _internal_retry_human_message,
)

EMPTY_RESPONSE_RETRY_LIMIT = 2
CUSTOM_TOOL_RESPONSE_RETRY_LIMIT = 2
DELEGATED_REPORT_RETRY_LIMIT = 2


@dataclass(frozen=True, slots=True)
class StepAgentRetryResult:
    final_text: str
    latest_model_content_blocks: tuple[dict[str, object], ...]
    token_usage_parts: tuple[Any, ...]
    successful_tool_calls: tuple[Any, ...]
    completed_custom_tool_names: tuple[str, ...]
    stream_result: AgentEventStreamResult


async def run_agent_with_retries(
    *,
    build_agent: StepAgentFactory,
    session_id: str,
    effective_job_id: str,
    resolved_agent_id: str,
    config: dict[str, Any],
    next_input_messages: list[Any],
    custom_tool_skill_sources: dict[str, list[str]],
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
    session_changes_service: SessionChangesRecorderProtocol,
    workspace_root: Path,
    model_timeout_seconds: float | None,
    message_stream_runtime: Any,
    turn_scope: Any,
    progress_reporter: Callable[[str], None] | None,
    requested_custom_tool_names: set[str],
    requires_delegated_report: bool,
    parent_session_id: str | None,
    delegated_report_allowed_kinds: frozenset[str],
    requires_session_question_reply: bool,
    question_sender_session_id: str | None,
    question_communication_id: str | None,
    execution_overrides: Mapping[str, object],
    model_visibility_overrides: Mapping[str, object],
    preferred_provider_id: str | None,
    include_team_tools: bool,
) -> StepAgentRetryResult:

    logger = logging.getLogger(__name__)

    logger.info(
        "[agent_execution_service] agent runtime build begin: job_id=%s",
        effective_job_id,
    )

    # runner 的 snapshot context 随 asyncio.to_thread 传播到唯一 Agent factory。
    agent = await asyncio.to_thread(
        build_agent,
        session_id=session_id,
        agent_id=resolved_agent_id,
        execution_overrides=execution_overrides,
        model_visibility_overrides=model_visibility_overrides,
        preferred_provider_id=preferred_provider_id,
        include_team_tools=include_team_tools,
    )

    logger.info(
        "[agent_execution_service] agent runtime ready: job_id=%s",
        effective_job_id,
    )
    if progress_reporter is not None:
        progress_reporter("agent_runtime_ready")
    logger.info(
        "[agent_execution_service] agent loop ready: job_id=%s",
        effective_job_id,
    )
    if progress_reporter is not None:
        progress_reporter("agent_loop_ready")
    empty_response_retries = 0
    custom_tool_response_retries = 0
    delegated_report_retries = 0
    turn_token_usage_parts: list[Any] = []
    successful_tool_calls: list[Any] = []
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
            publish=publish,
            session_changes_service=session_changes_service,
            workspace_root=workspace_root,
            message_stream_runtime=message_stream_runtime,
            cancellation_signal=turn_scope.cancellation_signal,
            execution_scope=turn_scope,
            model_timeout_seconds=model_timeout_seconds,
            progress_reporter=progress_reporter,
        )
        turn_token_usage_parts.append(stream_result.token_usage)
        final_text = stream_result.final_text
        successful_tool_calls.extend(stream_result.successful_tool_calls)
        completed_custom_tool_names.update(stream_result.completed_custom_tool_names)
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
            outcome=("accepted" if validation_succeeded else "validation_failed"),
            reason=(None if validation_succeeded else "AgentLoop 最终业务校验未通过"),
        )
        if validation_succeeded:
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
            await publish(
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
                allow_progress="progress" in delegated_report_allowed_kinds,
            )
            await publish(
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
            await publish(
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
        await publish(
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

    return StepAgentRetryResult(
        final_text=final_text,
        latest_model_content_blocks=latest_model_content_blocks,
        token_usage_parts=tuple(turn_token_usage_parts),
        successful_tool_calls=tuple(successful_tool_calls),
        completed_custom_tool_names=tuple(sorted(completed_custom_tool_names)),
        stream_result=stream_result,
    )


__all__ = ["StepAgentRetryResult", "run_agent_with_retries"]
