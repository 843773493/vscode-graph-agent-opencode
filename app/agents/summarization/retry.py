"""摘要生成辅助与 overflow 重试候选链。

承载摘要指令构造、媒体剥离、大型工具载荷压缩、overflow 重试中段切分、重试标记与
摘要文本校验；不修改 checkpoint 中的原消息。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import (
    Any,
    cast,
)

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)

from app.agents.summarization.state import (
    _MAX_SUMMARY_OVERFLOW_RETRIES,
    _MEDIA_BLOCK_MARKERS,
    _TOOL_PAYLOAD_RETRY_MAX_CHARS,
    _TOOL_PAYLOAD_RETRY_TOTAL_CHARS,
    CachePreservingPartition,
)
from app.prompting import internal_message_factory


def _summary_instruction(message_count: int) -> HumanMessage:
    prepared = internal_message_factory.build(
        kind="compaction_summary_instruction",
        control=(
            "CRITICAL: Return plain text only. Do not call tools; tool calls are "
            "rejected and make this compaction fail.\n\n"
            "Create a concise but complete summary of only the "
            f"{message_count} messages immediately before this instruction. "
            "Preserve user requests, completed work, files and code involved, "
            "decisions, identifiers, errors and fixes, constraints, unresolved "
            "work, and the precise next step. Do not summarize the earlier stable "
            "prefix. The summary must be substantially shorter than the messages it "
            "replaces: use compact factual bullets, omit conversational prose and "
            "repeated content, and never reproduce large source blocks. Return only "
            "the summary."
        ),
        metadata={"lc_source": "summarization_request"},
    )
    return HumanMessage(
        content=prepared.content,
        response_metadata=prepared.metadata,
    )


def _forked_summary_messages(
    partition: CachePreservingPartition,
    messages_to_summarize: list[AnyMessage] | None = None,
) -> list[AnyMessage]:
    middle = (
        partition.messages_to_summarize
        if messages_to_summarize is None
        else messages_to_summarize
    )
    return [
        *partition.prefix_messages,
        *middle,
        _summary_instruction(len(middle)),
    ]


def _strip_media_content(content: object) -> tuple[object, bool]:
    if not isinstance(content, list):
        return content, False

    stripped: list[object] = []
    changed = False
    for item in content:
        if not isinstance(item, dict):
            stripped.append(item)
            continue
        block_type = item.get("type")
        if isinstance(block_type, str) and block_type in _MEDIA_BLOCK_MARKERS:
            stripped.append({"type": "text", "text": _MEDIA_BLOCK_MARKERS[block_type]})
            changed = True
            continue
        if block_type == "tool_result" and "content" in item:
            nested, nested_changed = _strip_media_content(item["content"])
            if nested_changed:
                stripped.append({**item, "content": nested})
                changed = True
                continue
        stripped.append(item)
    return stripped, changed


def strip_media_from_summary_messages(
    messages: list[AnyMessage],
) -> tuple[list[AnyMessage], bool]:
    """仅为溢出重试剥离媒体，不修改 checkpoint 中的原消息。"""
    stripped_messages: list[AnyMessage] = []
    changed = False
    for message in messages:
        stripped_content, message_changed = _strip_media_content(message.content)
        if not message_changed:
            stripped_messages.append(message)
            continue
        stripped_messages.append(
            message.model_copy(
                update={
                    "content": cast(
                        "str | list[str | dict[str, Any]]",
                        stripped_content,
                    )
                }
            )
        )
        changed = True
    return stripped_messages, changed


def _content_character_count(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_content_character_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_content_character_count(item) for item in value)
    return len(str(value))


def compact_large_tool_payloads_for_summary(
    messages: list[AnyMessage],
) -> tuple[list[AnyMessage], bool]:
    """仅压缩 overflow 重试副本中的大型工具载荷，并保留调用配对。"""
    payloads: list[tuple[str, int, int, int]] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call_index, tool_call in enumerate(message.tool_calls):
                payloads.append(
                    (
                        "args",
                        message_index,
                        call_index,
                        _content_character_count(tool_call.get("args", {})),
                    )
                )
        elif isinstance(message, ToolMessage):
            payloads.append(
                (
                    "result",
                    message_index,
                    -1,
                    _content_character_count(message.content),
                )
            )

    marked = {
        (kind, message_index, call_index)
        for kind, message_index, call_index, size in payloads
        if size > _TOOL_PAYLOAD_RETRY_MAX_CHARS
    }
    remaining_size = sum(
        size
        for kind, message_index, call_index, size in payloads
        if (kind, message_index, call_index) not in marked
    )
    for kind, message_index, call_index, size in payloads:
        key = (kind, message_index, call_index)
        if remaining_size <= _TOOL_PAYLOAD_RETRY_TOTAL_CHARS:
            break
        if key in marked:
            continue
        marked.add(key)
        remaining_size -= size

    if not marked:
        return list(messages), False

    compacted: list[AnyMessage] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, ToolMessage) and ("result", message_index, -1) in marked:
            compacted.append(
                message.model_copy(
                    update={
                        "content": "[large tool result omitted for compaction retry]"
                    }
                )
            )
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_calls = []
            message_changed = False
            for call_index, tool_call in enumerate(message.tool_calls):
                if ("args", message_index, call_index) not in marked:
                    tool_calls.append(tool_call)
                    continue
                tool_calls.append(
                    {
                        **tool_call,
                        "args": {"_omitted": "large tool arguments"},
                    }
                )
                message_changed = True
            if message_changed:
                compacted.append(message.model_copy(update={"tool_calls": tool_calls}))
                continue
        compacted.append(message)
    return compacted, True


def _overflow_retry_middle_messages(
    messages: list[AnyMessage],
    safe_middle_boundaries: frozenset[int],
) -> list[list[AnyMessage]]:
    stripped, media_changed = strip_media_from_summary_messages(messages)
    retries: list[list[AnyMessage]] = [stripped] if media_changed else []
    compacted, tool_payload_changed = compact_large_tool_payloads_for_summary(stripped)
    if tool_payload_changed:
        retries.append(compacted)
    else:
        compacted = stripped
    boundaries = [
        index
        for index in range(2, len(compacted))
        if index in safe_middle_boundaries
    ]
    selected: set[int] = set()
    for attempt in range(1, _MAX_SUMMARY_OVERFLOW_RETRIES + 1):
        target = (len(compacted) * attempt) // (_MAX_SUMMARY_OVERFLOW_RETRIES + 1)
        boundary = next((index for index in boundaries if index >= target), None)
        if boundary is None or boundary in selected:
            continue
        selected.add(boundary)
        suffix = compacted[boundary:]
        marker = (
            []
            if suffix and isinstance(suffix[0], HumanMessage)
            else [_compaction_retry_marker()]
        )
        retries.append(
            [
                *marker,
                *suffix,
            ]
        )
    return retries


def _compaction_retry_marker() -> HumanMessage:
    prepared = internal_message_factory.build(
        kind="compaction_retry_marker",
        control="[earlier conversation truncated for compaction retry]",
        metadata={"lc_source": "summarization_retry"},
    )
    return HumanMessage(
        content=prepared.content,
        response_metadata=prepared.metadata,
    )


def validate_summary_text(summary: str) -> str:
    """拒绝第三方摘要器用普通字符串伪装的失败结果。"""
    normalized = summary.strip()
    if not normalized:
        raise ValueError("压缩摘要模型返回了空文本")
    # TODO: DeepAgents 改为抛出摘要异常后，删除对其错误字符串的显式识别。
    if normalized.startswith("Error generating summary:"):
        raise RuntimeError(f"压缩摘要生成失败: {normalized}")
    if normalized == "Previous conversation was too long to summarize.":
        raise RuntimeError("压缩摘要输入在预处理后为空，无法生成有效摘要")
    return normalized
