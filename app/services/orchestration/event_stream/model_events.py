"""累计当前模型事件的临时 content parts 与 token usage，不创建 durable item。"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import cast

from app.schemas.event import ModelTokenUsagePayload
from app.services.mapping.agent_content_mapper import AgentStreamContentPart

_TEXT_FIELD_BY_BLOCK_TYPE = {
    "reasoning_content": "reasoning_content",
    "thinking": "thinking",
    "reasoning": "reasoning",
    "refusal": "refusal",
}
_TEXT_CHUNKS_KEY = "_stream_text_chunks"


def _usage_token_count(
    usage: Mapping[str, object],
    key: str,
) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"模型 usage_metadata.{key} 必须是非负整数，实际值: {value!r}")
    return value


def stream_chunk_token_usage(chunk: object) -> ModelTokenUsagePayload | None:
    message = getattr(chunk, "message", None)
    usage_metadata = getattr(message or chunk, "usage_metadata", None)
    if usage_metadata is None:
        return None
    if not isinstance(usage_metadata, Mapping):
        raise TypeError(
            "模型 chunk usage_metadata 必须是 mapping，"
            f"实际类型: {type(usage_metadata).__name__}"
        )

    raw_input_details = usage_metadata.get("input_token_details")
    cache_read_input_tokens: int | None = None
    if raw_input_details is not None:
        if not isinstance(raw_input_details, Mapping):
            raise TypeError(
                "模型 usage_metadata.input_token_details 必须是 mapping，"
                f"实际类型: {type(raw_input_details).__name__}"
            )
        if "cache_read" in raw_input_details:
            cache_read_input_tokens = _usage_token_count(
                raw_input_details,
                "cache_read",
            )

    return ModelTokenUsagePayload(
        input_tokens=_usage_token_count(usage_metadata, "input_tokens"),
        output_tokens=_usage_token_count(usage_metadata, "output_tokens"),
        total_tokens=_usage_token_count(usage_metadata, "total_tokens"),
        cache_read_input_tokens=cache_read_input_tokens,
        model_calls=1,
        reported_model_calls=1,
    )


def last_model_token_usage(
    usages: Sequence[ModelTokenUsagePayload],
) -> ModelTokenUsagePayload:
    """返回最后一次实际模型请求的 token 统计，不跨请求累计。"""
    return next(
        (
            usage.model_copy(deep=True)
            for usage in reversed(usages)
            if usage.model_calls > 0
        ),
        ModelTokenUsagePayload(),
    )


def merge_model_content_part(
    part: AgentStreamContentPart,
    *,
    part_ids_in_order: list[str],
    parts_by_id: dict[str, dict[str, object]],
) -> None:
    """把规范化 model block/delta 合并到本次请求的有序内存视图。"""
    existing = parts_by_id.get(part.part_id)
    text_key = _TEXT_FIELD_BY_BLOCK_TYPE.get(part.block_type, "text")
    if existing is None:
        part_ids_in_order.append(part.part_id)
        initial: dict[str, object] = {
            "type": part.block_type,
            "id": part.part_id,
            "index": part.index,
        }
        if part.block_type == "reasoning_items":
            items = part.reasoning_items
            initial["reasoning_items"] = (
                copy.deepcopy(items) if isinstance(items, list) else []
            )
        elif part.block_type == "redacted_thinking":
            data = part.redacted_thinking_data
            if isinstance(data, str):
                initial["data"] = data
        else:
            initial[text_key] = part.text
            initial[_TEXT_CHUNKS_KEY] = [part.text]
        parts_by_id[part.part_id] = initial
        if part.extras:
            parts_by_id[part.part_id]["extras"] = dict(part.extras)
        return
    if existing.get("type") != part.block_type or existing.get("index") != part.index:
        raise RuntimeError(
            f"模型流 part 身份冲突: part_id={part.part_id} "
            f"existing={existing!r} incoming={part!r}"
        )
    if part.block_type == "reasoning_items":
        current_items = existing.get("reasoning_items")
        incoming_items = part.reasoning_items
        if not isinstance(current_items, list) or not isinstance(incoming_items, list):
            raise TypeError(f"模型流 part 缺少 reasoning_items: part_id={part.part_id}")
        current_item_indexes: dict[str, int] = {}
        for item_position, current in enumerate(current_items):
            current_id = current.get("id") if isinstance(current, dict) else None
            if isinstance(current_id, str):
                current_item_indexes.setdefault(current_id, item_position)
        for incoming in incoming_items:
            incoming_id = incoming.get("id") if isinstance(incoming, dict) else None
            if isinstance(incoming_id, str):
                item_position = current_item_indexes.get(incoming_id)
                if item_position is not None:
                    current_items[item_position] = copy.deepcopy(incoming)
                else:
                    item_position = len(current_items)
                    current_items.append(copy.deepcopy(incoming))
                    current_item_indexes[incoming_id] = item_position
            elif incoming not in current_items:
                current_items.append(copy.deepcopy(incoming))
    elif part.block_type == "redacted_thinking":
        data = part.redacted_thinking_data
        if isinstance(data, str):
            existing["data"] = data
    else:
        text_chunks = existing.get(_TEXT_CHUNKS_KEY)
        if not isinstance(text_chunks, list):
            raise TypeError(f"模型流 part 缺少 {_TEXT_CHUNKS_KEY}: part_id={part.part_id}")
        text_chunks.append(part.text)
    if part.extras:
        existing_extras = existing.get("extras")
        merged_extras = (
            dict(existing_extras) if isinstance(existing_extras, dict) else {}
        )
        merged_extras.update(part.extras)
        existing["extras"] = merged_extras


def materialize_model_content_parts(
    part_ids_in_order: list[str],
    parts_by_id: dict[str, dict[str, object]],
) -> tuple[dict[str, object], ...]:
    """将流式文本片段一次拼接为最终模型内容块。"""
    result: list[dict[str, object]] = []
    for part_id in part_ids_in_order:
        part = parts_by_id.get(part_id)
        if part is None:
            continue
        text_chunks = part.pop(_TEXT_CHUNKS_KEY, None)
        if text_chunks is not None:
            block_type = part.get("type")
            text_key = (
                _TEXT_FIELD_BY_BLOCK_TYPE.get(block_type, "text")
                if isinstance(block_type, str)
                else "text"
            )
            part[text_key] = "".join(cast(list[str], text_chunks))
        result.append(part)
    return tuple(result)
