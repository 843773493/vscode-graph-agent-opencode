"""有序历史 projection 纯投影，不执行 I/O。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.turn import (
    TurnDetailDTO,
    TurnSummaryDTO,
    TurnThinkingBlockDTO,
    TurnUserMessageDTO,
    TurnUserMessageSummaryDTO,
)


@dataclass(frozen=True, slots=True)
class HistoryProjectionLimits:
    fragments: int = 512
    bytes: int = 4 * 1024 * 1024
    chars: int = 256 * 1024
    item_chars: int = 64 * 1024

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.fragments,
                self.bytes,
                self.chars,
                self.item_chars,
            )
        ):
            raise ValueError("历史读取 limits 必须全部为正整数")


@dataclass(slots=True)
class HistoryProjectionBudget:
    """单页展示投影的预算；每次加入文本或 JSON 片段时记账。"""

    limits: HistoryProjectionLimits = field(default_factory=HistoryProjectionLimits)
    fragments_used: int = 0
    bytes_used: int = 0
    chars_used: int = 0

    def can_add(self, *, byte_count: int, char_count: int) -> bool:
        return (
            self.fragments_used < self.limits.fragments
            and byte_count <= self.limits.item_chars
            and char_count <= self.limits.item_chars
            and self.bytes_used + byte_count <= self.limits.bytes
            and self.chars_used + char_count <= self.limits.chars
        )

    def add(self, *, byte_count: int, char_count: int) -> None:
        if not self.can_add(byte_count=byte_count, char_count=char_count):
            raise ValueError("详情读取预算已耗尽")
        self.fragments_used += 1
        self.bytes_used += byte_count
        self.chars_used += char_count


def project_detail(
    detail: TurnDetailDTO,
    include: tuple[str, ...],
    budget: HistoryProjectionBudget,
) -> TurnDetailDTO:
    fields = set(include)
    user_messages = detail.user_messages if "user" in fields else []
    if "internal" in fields:
        user_messages = detail.user_messages
    if "metadata" not in fields:
        user_messages = [
            message.model_copy(update={"metadata": {}}) for message in user_messages
        ]
    projected_user_messages: list[TurnUserMessageDTO] = []
    output_truncated = False
    for message in user_messages:
        content, content_truncated = bounded_text(message.content, budget)
        if content_truncated:
            output_truncated = True
        projected_user_messages.append(
            message.model_copy(
                update={
                    "content": content,
                    "content_truncated": (
                        message.content_truncated or content_truncated
                    ),
                }
            )
        )
    items: list[TraceEventDTO] = []
    tool_detail_requested = bool(fields & {"tool_summary", "tool_call", "tool_result"})
    expected_items = 0
    for item in detail.items:
        is_call = item.type == "tool_call_start"
        requested = "tool_call" in fields if is_call else "tool_result" in fields
        summary_requested = "tool_summary" in fields
        if not requested and not summary_requested:
            continue
        expected_items += 1
        raw = item.raw if requested else {}
        content = item.content if requested else ""
        bounded_raw = raw
        if requested:
            encoded = json.dumps(raw, ensure_ascii=False, default=str)
            if not budget.can_add(
                byte_count=len(encoded.encode("utf-8")),
                char_count=len(encoded),
            ):
                output_truncated = True
                break
            budget.add(
                byte_count=len(encoded.encode("utf-8")),
                char_count=len(encoded),
            )
            content, content_truncated = bounded_text(content, budget)
            if content_truncated:
                output_truncated = True
        items.append(item.model_copy(update={"raw": bounded_raw, "content": content}))
    final_response = ""
    final_response_truncated = False
    if "final_response" in fields or "assistant" in fields:
        final_response, content_truncated = bounded_text(
            detail.final_response,
            budget,
        )
        final_response_truncated = content_truncated
        output_truncated = output_truncated or content_truncated
    assistant_text: list[str] = []
    if "assistant_text" in fields or "assistant" in fields:
        for value in detail.assistant_text:
            bounded, content_truncated = bounded_text(value, budget)
            assistant_text.append(bounded)
            output_truncated = output_truncated or content_truncated
    thinking_blocks: list[TurnThinkingBlockDTO] = []
    if fields & {"thinking", "reasoning_summary", "reasoning_detail"}:
        for block in detail.thinking_blocks:
            if block.kind == "encrypted" and "encrypted_reasoning_meta" in fields:
                thinking_blocks.append(block)
                continue
            if block.kind == "encrypted":
                continue
            if block.kind == "reasoning" and not fields & {
                "reasoning_detail",
                "thinking",
            }:
                continue
            if block.kind == "summary" and not fields & {
                "reasoning_summary",
                "reasoning_detail",
                "thinking",
            }:
                continue
            bounded, content_truncated = bounded_text(block.text, budget)
            thinking_blocks.append(block.model_copy(update={"text": bounded}))
            output_truncated = output_truncated or content_truncated
    response_parts = []
    for part in detail.response_parts:
        # 非 completed 终态 Turn 没有 final pointer，已生成的 assistant 正文
        # 是该 Turn 唯一可展示的答复记录；与用户中断的 partial 正文一样按
        # final_response 语义进入默认摘要投影，避免正文在历史中丢失。
        terminal_without_final = part.kind == "text" and detail.status in {
            "failed",
            "cancelled",
            "timed_out",
        }
        requested = (
            "metadata"
            if part.kind == "context_notice"
            else "final_response"
            if part.kind == "final_text"
            or (part.partial is True and part.completion_reason == "user_interrupt")
            or terminal_without_final
            else "text"
            if part.kind == "text"
            else "reasoning_detail"
            if part.kind == "reasoning"
            else "reasoning_summary"
            if part.kind == "reasoning_summary"
            else "encrypted_reasoning_meta"
            if part.kind == "reasoning_encrypted"
            else "tool_call"
            if part.kind == "tool_call"
            else "tool_result"
        )
        reasoning_requested = (
            (
                part.kind == "reasoning"
                and bool(fields & {"thinking", "reasoning_detail"})
            )
            or (
                part.kind == "reasoning_summary"
                and bool(fields & {"thinking", "reasoning_summary", "reasoning_detail"})
            )
            or (
                part.kind == "reasoning_encrypted"
                and "encrypted_reasoning_meta" in fields
            )
        )
        if (
            requested not in fields
            and not (requested == "final_response" and "assistant" in fields)
            and not reasoning_requested
            and not (
                requested in {"tool_call", "tool_result"}
                and "tool_summary" in fields
            )
        ):
            continue
        text, content_truncated = bounded_text(part.text, budget)
        response_parts.append(part.model_copy(update={"text": text}))
        output_truncated = output_truncated or content_truncated
    return detail.model_copy(
        update={
            "user_messages": projected_user_messages,
            "final_response": final_response,
            "assistant_text": assistant_text,
            "thinking_blocks": thinking_blocks,
            # 显式详情超出预算时仍保留工具名和状态，避免 UI 只得到
            # detail_truncated=true 却失去可解释的工具摘要。
            "tool_summary": (
                detail.tool_summary
                if "tool_summary" in fields
                or (tool_detail_requested and output_truncated)
                else []
            ),
            "response_preview": final_response[:1000],
            "preview_truncated": (
                final_response_truncated or len(final_response) > 1000
            ),
            "items": items,
            "response_parts": response_parts,
            "detail_truncated": output_truncated
            or detail.detail_truncated
            or (tool_detail_requested and len(items) < expected_items),
        }
    )


def bounded_text(
    value: str,
    budget: HistoryProjectionBudget,
) -> tuple[str, bool]:
    if not value:
        return "", False
    limit = min(len(value), budget.limits.item_chars)
    candidate = value[:limit]
    encoded = json.dumps(candidate, ensure_ascii=False)
    while candidate and not budget.can_add(
        byte_count=len(encoded.encode("utf-8")),
        char_count=len(encoded),
    ):
        limit //= 2
        candidate = value[:limit]
        encoded = json.dumps(candidate, ensure_ascii=False)
    if not budget.can_add(
        byte_count=len(encoded.encode("utf-8")),
        char_count=len(encoded),
    ):
        return "", True
    budget.add(
        byte_count=len(encoded.encode("utf-8")),
        char_count=len(encoded),
    )
    return candidate, len(candidate) < len(value)


def summary(detail: TurnDetailDTO) -> TurnSummaryDTO:
    return TurnSummaryDTO(
        **detail.model_dump(
            mode="python",
            include={
                "turn_id",
                "job_id",
                "session_id",
                "ordinal",
                "root_logical_item_ordinal",
                "source_view_id",
                "source_view_revision",
                "revision",
                "status",
                "created_at",
                "updated_at",
                "completed_at",
            },
        ),
        source_message_ids=detail.source_message_ids,
        source_message_count=len(detail.source_message_ids),
        user_messages=[
            TurnUserMessageSummaryDTO(
                message_id=item.message_id,
                preview=item.content[:500],
                content_truncated=len(item.content) > 500,
                attachment_count=len(item.attachments),
                created_at=item.created_at,
            )
            for item in detail.user_messages
        ],
        user_message_count=len(detail.user_messages),
        response_preview=detail.response_preview,
        preview_truncated=detail.preview_truncated,
        item_count=detail.activity_stats.item_count,
        thinking_blocks=detail.thinking_blocks,
        tool_summary=detail.tool_summary,
        tool_summary_truncated=detail.tool_summary_truncated,
        response_parts=[
            part.model_copy(
                update={
                    "projection": "summary",
                    "arguments": None if part.kind == "tool_call" else part.arguments,
                    "result": None if part.kind == "tool_result" else part.result,
                    # summary 契约与 mapper 首段投影一致：工具部件不携带
                    # 正文；detail 投影降级为摘要时同样剥掉，避免 summaries
                    # 与默认 tail 摘要口径分裂。
                    "text": ""
                    if part.kind in {"tool_call", "tool_result"}
                    else part.text,
                }
            )
            # 空正文的来源标记不挤占普通内容预算，也不能因附在详情末尾而被截掉。
            for index, part in enumerate(detail.response_parts)
            if index < 128 or part.kind == "context_notice"
        ],
        activity_stats=detail.activity_stats,
    )
