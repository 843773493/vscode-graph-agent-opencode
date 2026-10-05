"""将 canonical reasoning item 合并到 LangChain agent-state 投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.carrier_dedup import (
    normalized_tool_call_ids,
    superseded_stream_content_item_ids,
)


def _block_index(block: Mapping[str, object]) -> int | None:
    index = block.get("index")
    if index is None:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("reasoning projection 的 content-part index 非法")
    return index


def _part_coordinate(
    block: Mapping[str, object],
    model_call_id: str,
) -> tuple[str, int] | None:
    part_id, index = block.get("id"), _block_index(block)
    if not isinstance(part_id, str) or not part_id or index is None:
        return None
    return part_id.removeprefix(f"{model_call_id}:block:"), index


def _merge_content(
    content: str | list[str | dict],
    canonical_blocks: list[dict[str, object]],
    model_call_id: str,
) -> tuple[object, str | None]:
    if isinstance(content, str):
        existing = [{"type": "text", "text": content}] if content else []
    else:
        if any(not isinstance(block, Mapping) for block in content):
            raise TypeError("reasoning merge 只接受已规范化的 content carrier")
        existing = [dict(block) for block in content]
    existing_coordinates = {
        coordinate
        for block in existing
        if (coordinate := _part_coordinate(block, model_call_id)) is not None
    }
    missing = [
        block
        for block in canonical_blocks
        if _part_coordinate(block, model_call_id) not in existing_coordinates
    ]
    if not missing:
        return existing, None
    if not existing:
        return missing, None
    if any(_block_index(block) is None for block in [*existing, *missing]):
        # 展示投影保留原 carrier，并明确报告缺失坐标；正文不能替代 part 身份。
        return content, "content_part_index_missing"
    merged = list(existing)
    after_previous = 0
    for block in missing:
        index = _block_index(block)
        position = next(
            (i for i, value in enumerate(merged) if _block_index(value) > index),
            len(merged),
        )
        if position < after_previous:
            return content, "content_part_order_conflict"
        merged.insert(position, block)
        after_previous = position + 1
    return merged, None


def merge_canonical_reasoning(
    raw_messages: Sequence[object],
    canonical_items: Sequence[CanonicalItemRecord],
) -> list[object]:
    """只修改临时 agent-state view，身份只取真实 model-call 与 part 坐标。"""
    superseded = superseded_stream_content_item_ids(canonical_items)
    reasoning: dict[str, dict[tuple[str, int], dict[str, object]]] = {}
    tools: dict[str, set[str]] = {}
    for item in canonical_items:
        identity = item.projection_identity
        if identity is None or identity.model_call_id is None:
            continue
        scope = identity.model_call_id
        if item.semantic_kind == SemanticKind.TOOL_CALL:
            tools.setdefault(scope, set()).update(normalized_tool_call_ids(item))
        elif (
            item.semantic_kind == SemanticKind.REASONING
            and item.item_id not in superseded
            and item.status in {"completed", "partial"}
            and isinstance(item.payload, str)
            and item.payload
            and identity.block_id is not None
            and identity.block_index is not None
        ):
            coordinate = (identity.block_id, identity.block_index)
            blocks = reasoning.setdefault(scope, {})
            blocks.setdefault(
                coordinate,
                {
                    "type": "reasoning",
                    "reasoning": item.payload,
                    "id": identity.block_id,
                    "index": identity.block_index,
                },
            )
    if not reasoning:
        return list(raw_messages)

    result = list(raw_messages)
    for index, message in enumerate(result):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        metadata = dict(message.response_metadata or {})
        scope = metadata.get("model_call_id")
        call_ids = {
            call["id"]
            for call in message.tool_calls
            if isinstance(call, Mapping) and isinstance(call.get("id"), str)
        }
        if not isinstance(scope, str) or not scope:
            if call_ids & set().union(*tools.values()):
                metadata.update(
                    reasoning_merge_status="skipped_ambiguous_order",
                    reasoning_merge_reason="model_call_scope_missing",
                )
                result[index] = message.model_copy(
                    update={"response_metadata": metadata}
                )
            continue
        canonical_blocks = list(reasoning.get(scope, {}).values())
        scope_identity = ItemProjectionIdentity(model_call_id=scope)
        canonical_call_ids = {
            provider_tool_call_id(scope_identity, call_id) for call_id in call_ids
        }
        if not canonical_blocks or not canonical_call_ids & tools.get(scope, set()):
            continue
        metadata["reasoning_source"] = "canonical_item_stream"
        merged, merge_status = _merge_content(message.content, canonical_blocks, scope)
        if merge_status is not None:
            metadata.update(
                reasoning_merge_status="skipped_ambiguous_order",
                reasoning_merge_reason=merge_status,
            )
        result[index] = message.model_copy(
            update={
                "content": merged,
                "response_metadata": metadata,
            }
        )
    return result


__all__ = ["merge_canonical_reasoning"]
