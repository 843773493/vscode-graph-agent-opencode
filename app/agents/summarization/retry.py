"""摘要生成辅助与 overflow 重试候选链。

承载摘要指令构造、媒体剥离、大型工具载荷压缩、overflow 重试中段切分、重试标记与
摘要文本校验；不修改 checkpoint 中的原消息。"""

from __future__ import annotations

from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
)

from app.agents.summarization.state import (
    _MAX_SUMMARY_OVERFLOW_RETRIES,
    CachePreservingPartition,
    SummaryGenerationError,
    SummaryTextError,
)
from app.prompting import internal_message_factory
from app.services.mapping.itemized.canonical_langchain import (
    compact_large_tool_payloads_for_summary,
    strip_media_from_summary_messages,
)


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
        raise SummaryTextError("压缩摘要模型返回了空文本")
    # TODO: DeepAgents 改为抛出摘要异常后，删除对其错误字符串的显式识别。
    if normalized.startswith("Error generating summary:"):
        raise SummaryGenerationError(f"压缩摘要生成失败: {normalized}")
    if normalized == "Previous conversation was too long to summarize.":
        raise SummaryGenerationError("压缩摘要输入在预处理后为空，无法生成有效摘要")
    return normalized
