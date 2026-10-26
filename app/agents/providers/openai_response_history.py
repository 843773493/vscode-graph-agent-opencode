"""OpenAI Responses history/tool pairing and response utility owner。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata

from app.runtime.provider_errors import mark_provider_failure
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.mapping.itemized.provider_request import (
    project_responses_tool_content,
)

logger = logging.getLogger(__name__)

DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS = 45.0


def _without_server_state(item: dict[str, Any]) -> dict[str, Any]:
    """store=false 时仅回放可移植的 Response item 内容。"""
    result = dict(item)
    result.pop("id", None)
    result.pop("status", None)
    result.pop("index", None)
    return result


def _responses_usage_metadata(usage: Any) -> UsageMetadata:
    raw = (
        dict(usage)
        if isinstance(usage, dict)
        else usage.model_dump(exclude_none=True)
        if hasattr(usage, "model_dump")
        else {}
    )
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    metadata: UsageMetadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(raw.get("total_tokens") or input_tokens + output_tokens),
    }
    details = raw.get("input_tokens_details") or {}
    input_details: InputTokenDetails = {}
    if details.get("cached_tokens") is not None:
        input_details["cache_read"] = int(details["cached_tokens"])
    if details.get("cache_write_tokens") is not None:
        input_details["cache_creation"] = int(details["cache_write_tokens"])
    if input_details:
        metadata["input_token_details"] = input_details
    return metadata


def _reasoning_summary_content(block: dict[str, Any]) -> dict[str, Any]:
    """把 Responses summary 转成可流式聚合的 reasoning content。"""

    if block.get("content"):
        return block
    summary_text = extract_reasoning_summary(block.get("summary"))
    if summary_text:
        block["content"] = [
            {
                "type": "reasoning_text",
                "text": summary_text,
            }
        ]
    return block


class ResponsesToolHistoryError(ValueError):
    """Responses API 历史中的工具调用/结果无法安全配对。"""


class ResponsesStreamOpenTimeoutError(TimeoutError):
    """Responses provider 在有限时间内没有建立可读取的事件流。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        mark_provider_failure(self)


class ResponsesPayloadBuildTimeoutError(TimeoutError):
    """Responses 历史/附件 payload 在有限时间内没有完成投影。"""


def _tool_call_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get("call_id") or value.get("id")
    return candidate if isinstance(candidate, str) and candidate else None


def _content_tool_calls(content: Any) -> list[dict[str, Any]]:
    """从旧版 content block 恢复 LangChain 的标准 tool_calls。"""
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") not in {
            "function_call",
            "tool_call",
        }:
            continue
        call_id = _tool_call_id(block)
        name = block.get("name")
        if call_id is None or not isinstance(name, str) or not name:
            raise ResponsesToolHistoryError(
                "Responses 历史中的 content tool call 缺少有效 call_id 或工具名: "
                f"index={index}"
            )
        raw_args = block.get("arguments", block.get("args", {}))
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as error:
                raise ResponsesToolHistoryError(
                    "Responses 历史中的 content tool call arguments 不是有效 JSON: "
                    f"call_id={call_id}"
                ) from error
        else:
            args = raw_args
        if not isinstance(args, dict):
            raise ResponsesToolHistoryError(
                "Responses 历史中的 content tool call arguments 必须是对象: "
                f"call_id={call_id}"
            )
        calls.append(
            {
                "name": name,
                "args": args,
                "id": call_id,
                "type": "tool_call",
            }
        )
    return calls


def _message_with_standard_tool_calls(message: AIMessage) -> AIMessage:
    """把散落在 content/additional_kwargs 的工具声明统一到 tool_calls。"""
    raw_additional = message.additional_kwargs or {}
    additional_calls = raw_additional.get("tool_calls")
    legacy_call = raw_additional.get("function_call")
    content_calls: list[dict[str, Any]] | None = None

    if not (
        (isinstance(additional_calls, list) and additional_calls)
        or isinstance(legacy_call, dict)
    ):
        seen_ids: set[str] = set()
        for call in message.tool_calls:
            if not isinstance(call, dict):
                break
            call_id = _tool_call_id(call)
            if call_id is None or call_id in seen_ids:
                break
            seen_ids.add(call_id)
        else:
            content_calls = _content_tool_calls(message.content)
            if not content_calls:
                return message

    calls: list[dict[str, Any]] = [
        dict(call) for call in message.tool_calls if isinstance(call, dict)
    ]
    if isinstance(additional_calls, list):
        calls.extend(dict(call) for call in additional_calls if isinstance(call, dict))
    if isinstance(legacy_call, dict):
        calls.append(dict(legacy_call))
    calls.extend(
        content_calls
        if content_calls is not None
        else _content_tool_calls(message.content)
    )
    unique_calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for call in calls:
        call_id = _tool_call_id(call)
        if call_id is None or call_id in seen_ids:
            continue
        seen_ids.add(call_id)
        unique_calls.append(call)
    if unique_calls == message.tool_calls:
        return message
    return message.model_copy(update={"tool_calls": unique_calls})


def _is_internal_history_message(message: BaseMessage) -> bool:
    """识别不应再次发送给 provider 的 checkpoint 内部提醒。"""
    metadata = message.response_metadata or {}
    if metadata.get("internal") is True:
        return True
    message_metadata = metadata.get("message_metadata")
    if (
        isinstance(message_metadata, Mapping)
        and message_metadata.get("internal") is True
    ):
        return True
    content = message.content
    return (
        isinstance(message, HumanMessage)
        and isinstance(content, str)
        and content.lstrip().startswith("<system_reminder>")
    )


def _project_tool_history(
    messages: Sequence[BaseMessage],
    *,
    deadline: float | None = None,
) -> list[BaseMessage]:
    """按一次连续模型回合投影工具历史，隔离跨 job 的未完成调用。

    LangGraph 会把同一 session 的 checkpoint 传给下一次 AgentLoop。旧 job
    在模型已经声明 tool call、但还没有写入 ToolMessage 时，新的用户消息仍
    可能紧接着追加到同一 messages channel。Responses API 不接受这种悬挂的
    function_call；更糟的是，若只用全局 ``declared`` 集合校验，后续旧结果
    还会被误认为已配对，并把非法 ``function_call_output`` 发到 provider。

    一个工具结果只允许匹配当前连续工具段中尚未消费的声明。旧 job 已写入的
    内部取消 reminder 是跨 job 的明确边界；该边界之前的旧工具事务不再重放，
    但没有该 reminder 的正常多轮历史仍完整保留。没有任何活动声明的
    ToolMessage 会被隔离并记录诊断，不进入本次 Responses 请求；否则一个
    旧失败 turn 的延迟结果就会阻断同一会话的后续消息。
    """

    projected_messages: list[BaseMessage] = []
    last_internal_message_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if _is_internal_history_message(messages[index]):
            last_internal_message_index = index
            break
    pending_tool_call_ids: set[str] = set()
    discarded_previous_tool_call_ids: set[str] = set()
    pending_tool_segment_start: int | None = None
    completed_tool_call_ids: set[str] = set()
    repaired_segments = 0
    orphaned_tool_results: list[tuple[str, int]] = []

    def discard_pending_segment() -> None:
        nonlocal pending_tool_segment_start, repaired_segments
        if pending_tool_segment_start is None:
            pending_tool_call_ids.clear()
            return
        del projected_messages[pending_tool_segment_start:]
        pending_tool_call_ids.clear()
        pending_tool_segment_start = None
        repaired_segments += 1

    for index, message in enumerate(messages):
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponsesPayloadBuildTimeoutError(
                "Responses 历史工具投影超过时间预算: "
                f"message_index={index} timeout_seconds="
                f"{DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
            )
        if _is_internal_history_message(message):
            if pending_tool_call_ids:
                discard_pending_segment()
            continue
        if isinstance(message, AIMessage):
            normalized = _message_with_standard_tool_calls(message)
            if index < last_internal_message_index and normalized.tool_calls:
                discarded_previous_tool_call_ids.update(
                    call_id
                    for call in normalized.tool_calls
                    if (call_id := _tool_call_id(call)) is not None
                )
                # 旧 turn 的 function_call 可能在 provider 侧仍引用已失效
                # 的 Responses call。当前 turn 不需要重放这些执行事务；完整
                # 工具配对只在当前 turn 内保留，避免跨 job 污染新请求。
                continue
            if pending_tool_call_ids:
                discard_pending_segment()
            message_tool_call_ids: set[str] = set()
            for call in normalized.tool_calls:
                if not isinstance(call, dict):
                    continue
                call_id = _tool_call_id(call)
                if call_id is None:
                    continue
                if call_id in completed_tool_call_ids:
                    raise ResponsesToolHistoryError(
                        "Responses 历史包含重复的工具调用 ID；无法区分当前与旧 job: "
                        f"call_id={call_id}, index={index}。请重建当前会话上下文后重试。"
                    )
                if call_id in message_tool_call_ids:
                    raise ResponsesToolHistoryError(
                        "Responses 历史的同一 assistant 消息包含重复工具调用 ID: "
                        f"call_id={call_id}, index={index}"
                    )
                message_tool_call_ids.add(call_id)
            projected_messages.append(normalized)
            if message_tool_call_ids:
                pending_tool_segment_start = len(projected_messages) - 1
                pending_tool_call_ids.update(message_tool_call_ids)
            continue

        if isinstance(message, ToolMessage):
            if (
                index < last_internal_message_index
                and message.tool_call_id in discarded_previous_tool_call_ids
            ):
                continue
            call_id = message.tool_call_id
            if not isinstance(call_id, str) or not call_id:
                orphaned_tool_results.append(("<missing>", index))
                continue
            if call_id not in pending_tool_call_ids:
                orphaned_tool_results.append((call_id, index))
                continue
            projected_messages.append(_portable_tool_result_message(message))
            pending_tool_call_ids.remove(call_id)
            completed_tool_call_ids.add(call_id)
            if not pending_tool_call_ids:
                pending_tool_segment_start = None
            continue

        if pending_tool_call_ids:
            # 当前用户消息意味着旧工具段已经跨越 job 边界；不能把它和
            # 新请求混在一起发送给 Responses API。
            discard_pending_segment()
        projected_messages.append(message)

    if pending_tool_call_ids:
        discard_pending_segment()
    if repaired_segments:
        logger.warning(
            "Responses 历史投影丢弃未完成的旧工具段: segments=%s messages_before=%s "
            "messages_after=%s",
            repaired_segments,
            len(messages),
            len(projected_messages),
        )
    if orphaned_tool_results:
        orphaned_count = len(orphaned_tool_results)
        orphaned_preview = orphaned_tool_results[:20]
        logger.warning(
            "Responses 历史投影隔离未配对的旧工具结果，未发送给 provider: count=%s "
            "results=%s%s",
            orphaned_count,
            orphaned_preview,
            " (其余结果已省略)" if orphaned_count > len(orphaned_preview) else "",
        )
    return projected_messages


def _portable_tool_result_message(message: ToolMessage) -> ToolMessage:
    """与 sealed native request 共用同一工具媒体投影，不另拼正文。"""
    raw_path = (message.additional_kwargs or {}).get("read_file_path")
    content = project_responses_tool_content(
        message.content, path=raw_path if isinstance(raw_path, str) else None,
    )
    return message if content is message.content else message.model_copy(update={"content": content})
