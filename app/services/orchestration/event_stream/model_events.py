"""累计当前模型事件的临时 content parts 与 token usage，不创建 durable item。"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence

from app.schemas.event import ModelTokenUsagePayload
from app.services.mapping.agent_content_mapper import AgentStreamContentPart


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
    part_order: list[str],
    parts: dict[str, dict[str, object]],
) -> None:
    """把规范化 model block/delta 合并到本次请求的有序内存视图。"""
    existing = parts.get(part.part_id)
    text_key = (
        "reasoning_content"
        if part.block_type == "reasoning_content"
        else "thinking"
        if part.block_type == "thinking"
        else "reasoning"
        if part.block_type == "reasoning"
        else "refusal"
        if part.block_type == "refusal"
        else "text"
    )
    if existing is None:
        part_order.append(part.part_id)
        initial: dict[str, object] = {
            "type": part.block_type,
            "id": part.part_id,
            "index": part.index,
        }
        if part.block_type == "reasoning_items":
            items = part.payload.get("reasoning_items") if part.payload else None
            initial["reasoning_items"] = (
                copy.deepcopy(items) if isinstance(items, list) else []
            )
        elif part.block_type == "redacted_thinking":
            data = part.payload.get("data") if part.payload else None
            if isinstance(data, str):
                initial["data"] = data
        else:
            initial[text_key] = part.text
        parts[part.part_id] = initial
        if part.extras:
            parts[part.part_id]["extras"] = dict(part.extras)
        return
    if existing.get("type") != part.block_type or existing.get("index") != part.index:
        raise RuntimeError(
            f"模型流 part 身份冲突: part_id={part.part_id} "
            f"existing={existing!r} incoming={part!r}"
        )
    if part.block_type == "reasoning_items":
        current_items = existing.get("reasoning_items")
        incoming_items = part.payload.get("reasoning_items") if part.payload else None
        if not isinstance(current_items, list) or not isinstance(incoming_items, list):
            raise TypeError(f"模型流 part 缺少 reasoning_items: part_id={part.part_id}")
        for incoming in incoming_items:
            incoming_id = incoming.get("id") if isinstance(incoming, dict) else None
            if isinstance(incoming_id, str):
                replaced = False
                for index, current in enumerate(current_items):
                    if isinstance(current, dict) and current.get("id") == incoming_id:
                        current_items[index] = copy.deepcopy(incoming)
                        replaced = True
                        break
                if replaced:
                    continue
            if incoming not in current_items:
                current_items.append(copy.deepcopy(incoming))
    elif part.block_type == "redacted_thinking":
        data = part.payload.get("data") if part.payload else None
        if isinstance(data, str):
            existing["data"] = data
    else:
        current_text = existing.get(text_key)
        if not isinstance(current_text, str):
            raise TypeError(f"模型流 part 缺少 {text_key}: part_id={part.part_id}")
        existing[text_key] = current_text + part.text
    if part.extras:
        existing_extras = existing.get("extras")
        merged_extras = (
            dict(existing_extras) if isinstance(existing_extras, dict) else {}
        )
        merged_extras.update(part.extras)
        existing["extras"] = merged_extras
