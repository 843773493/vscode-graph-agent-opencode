"""将 canonical reasoning item 合并到 LangChain agent-state 投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.records import CanonicalItemRecord


def _block_index(block: Mapping[str, object]) -> int | None:
    index = block.get("index")
    if index is None:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("reasoning projection 的 content-part index 非法")
    return index


def _reasoning_text(block: Mapping[str, object]) -> str | None:
    if block.get("type") not in {
        "reasoning",
        "reasoning_content",
        "reasoning_items",
        "thinking",
        "redacted_thinking",
    }:
        return None
    for key in ("reasoning", "reasoning_content", "thinking", "summary", "text"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return None


def _merge_content(
    content: str | list[str | dict], canonical_blocks: list[dict[str, object]]
) -> list[dict[str, object]]:
    if isinstance(content, str):
        existing = [{"type": "text", "text": content}] if content else []
    else:
        if any(not isinstance(block, Mapping) for block in content):
            raise TypeError("reasoning merge 只接受已规范化的 content carrier")
        existing = [dict(block) for block in content]
    existing_ids = {
        block["id"] for block in existing if isinstance(block.get("id"), str)
    }
    matched_existing_indexes: set[int] = set()
    missing: list[dict[str, object]] = []
    for block in canonical_blocks:
        if isinstance(block.get("id"), str) and block["id"] in existing_ids:
            continue
        canonical_text = _reasoning_text(block)
        matching_index = next(
            (
                index
                for index, value in enumerate(existing)
                if index not in matched_existing_indexes
                and canonical_text is not None
                and _reasoning_text(value) == canonical_text
            ),
            None,
        )
        if matching_index is not None:
            # provider carrier 已经承载了同一段 reasoning，只是没有稳定 id/index；
            # exact text match 不需要猜测插入位置，也不能重复展示一份 reasoning。
            matched_existing_indexes.add(matching_index)
            continue
        missing.append(block)
    if not missing:
        return existing
    if not existing:
        return list(missing)
    if any(_block_index(block) is None for block in [*existing, *missing]):
        raise ValueError("reasoning merge 缺少 content-part 顺序，拒绝猜测插入位置")
    # 保留已有 carrier（包括保护字段）及 canonical 输入顺序；index 只定位
    # 同条消息中缺失 carrier 的插入点，绝不按物理 item_sequence 重排输入。
    merged = list(existing)
    after_previous = 0
    for block in missing:
        index = _block_index(block)
        position = next(
            (i for i, value in enumerate(merged) if _block_index(value) > index),
            len(merged),
        )
        if position < after_previous:
            raise ValueError(
                "reasoning merge 的 canonical 顺序与 content-part 顺序冲突"
            )
        merged.insert(position, block)
        after_previous = position + 1
    return merged


def merge_canonical_reasoning(
    raw_messages: Sequence[object],
    canonical_items: Sequence[CanonicalItemRecord],
) -> list[object]:
    """只修改临时 agent-state view，不回写 canonical item 或 checkpoint。"""
    ordered_reasoning: list[tuple[str, dict[str, object]]] = []
    tool_call_groups: dict[str, list[str]] = {}
    for item in canonical_items:
        producer_ref = item.producer_ref
        invocation_id = (
            producer_ref.get("invocation_id")
            if isinstance(producer_ref, Mapping)
            else None
        )
        group_id = (
            invocation_id
            if isinstance(invocation_id, str) and invocation_id
            else item.message_group_id or item.item_id
        )
        if item.semantic_kind == SemanticKind.REASONING:
            if item.status not in {"completed", "partial"}:
                continue
            if not isinstance(item.payload, str) or not item.payload:
                continue
            block: dict[str, object] = {"type": "reasoning", "reasoning": item.payload}
            block_id = item.metadata.get("block_id")
            block_index = item.metadata.get("block_index")
            if isinstance(block_id, str) and block_id:
                block["id"] = block_id
            if isinstance(block_index, int) and not isinstance(block_index, bool):
                block["index"] = block_index
            ordered_reasoning.append((group_id, block))
            continue
        if item.semantic_kind != SemanticKind.TOOL_CALL:
            continue
        payload = item.payload if isinstance(item.payload, Mapping) else {}
        raw_calls = payload.get("tool_calls")
        calls = raw_calls if isinstance(raw_calls, list) else [payload]
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            tool_call_id = call.get("id") or call.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                groups = tool_call_groups.setdefault(tool_call_id, [])
                if group_id not in groups:
                    groups.append(group_id)

    if not ordered_reasoning or not tool_call_groups:
        return list(raw_messages)

    result = list(raw_messages)
    for index, message in enumerate(result):
        if not isinstance(message, AIMessage):
            continue
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            continue
        groups = {
            group_id
            for call in tool_calls
            if isinstance(call, Mapping)
            for tool_call_id in (call.get("id"),)
            if isinstance(tool_call_id, str) and tool_call_id in tool_call_groups
            for group_id in tool_call_groups[tool_call_id]
        }
        if not groups:
            continue
        canonical_blocks = [
            block for group_id, block in ordered_reasoning if group_id in groups
        ]
        if not canonical_blocks:
            continue
        unique_blocks: list[dict[str, object]] = []
        for block in canonical_blocks:
            block_text = _reasoning_text(block)
            block_index = _block_index(block)
            duplicate = any(
                block_text is not None
                and _reasoning_text(previous) == block_text
                and (
                    block_index is None
                    or _block_index(previous) is None
                    or _block_index(previous) == block_index
                )
                for previous in unique_blocks
            )
            if duplicate:
                continue
            unique_blocks.append(block)
        metadata = dict(message.response_metadata or {})
        metadata["reasoning_source"] = "canonical_item_stream"
        result[index] = message.model_copy(
            update={
                "content": _merge_content(message.content, unique_blocks),
                "response_metadata": metadata,
            }
        )
    return result


__all__ = ["merge_canonical_reasoning"]
