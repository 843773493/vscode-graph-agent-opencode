"""Provider 流式 block 的无状态归一化。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from app.agents.providers.output_normalization import (
    append_unique,
    as_block,
    content_blocks,
    reasoning_content_block,
    reasoning_items_block,
)

_GENERATED_PART_PREFIX = "part_"


def append_stream_block(blocks: list[dict[str, Any]], block: dict[str, Any]) -> None:
    """合并相邻 reasoning delta，并按 provider item id 幂等更新。"""
    block_type = block.get("type")
    if blocks and block_type == "reasoning_content":
        previous = blocks[-1]
        if previous.get("type") == "reasoning_content":
            previous_text = previous.get("reasoning_content")
            current_text = block.get("reasoning_content")
            if isinstance(previous_text, str) and isinstance(current_text, str):
                previous["reasoning_content"] = previous_text + current_text
                return
    if blocks and block_type == "reasoning_items":
        previous = blocks[-1]
        if previous.get("type") == "reasoning_items":
            previous_items = previous.get("reasoning_items")
            current_items = block.get("reasoning_items")
            if isinstance(previous_items, list) and isinstance(current_items, list):
                for item in current_items:
                    item_id = item.get("id") if isinstance(item, Mapping) else None
                    if isinstance(item_id, str):
                        replaced = False
                        for index, previous_item in enumerate(previous_items):
                            if (
                                isinstance(previous_item, Mapping)
                                and previous_item.get("id") == item_id
                            ):
                                previous_items[index] = copy.deepcopy(item)
                                replaced = True
                                break
                        if replaced:
                            continue
                    if item not in previous_items:
                        previous_items.append(copy.deepcopy(item))
                return
    append_unique(blocks, block)


def _is_generated_part_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_GENERATED_PART_PREFIX)


def clean_stream_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """去除流式临时 part/index/extras，保留 provider item identity。"""
    result = {str(key): copy.deepcopy(value) for key, value in block.items()}
    extras = result.get("extras")
    provider_part_id = (
        extras.get("provider_part_id") if isinstance(extras, Mapping) else None
    )
    generated_id = _is_generated_part_id(result.get("id"))
    if generated_id:
        result.pop("id", None)
    if generated_id or provider_part_id is not None:
        result.pop("index", None)
    if isinstance(provider_part_id, str) and result.get("type") == "reasoning":
        result["id"] = provider_part_id
    return result


def stream_content_blocks(content: Any) -> list[dict[str, Any]]:
    """把已接收的 provider block 重新线性化为 canonical stream carrier。"""
    blocks: list[dict[str, Any]] = []
    for raw_block in content_blocks(content):
        block = clean_stream_block(raw_block)
        block_type = block.get("type")
        extras = block.get("extras")
        if block_type == "reasoning" and isinstance(extras, Mapping):
            response_item = as_block(extras.get("response_item"))
            thinking_block = as_block(extras.get("thinking_block"))
            if response_item is not None:
                append_stream_block(blocks, reasoning_items_block([response_item]))
                continue
            if thinking_block is not None:
                append_unique(blocks, thinking_block)
                continue
        block.pop("extras", None)
        if block_type == "reasoning" and isinstance(block.get("reasoning"), str):
            append_stream_block(blocks, reasoning_content_block(block["reasoning"]))
            continue
        if block_type == "reasoning":
            append_stream_block(blocks, reasoning_items_block([block]))
            continue
        if isinstance(block_type, str) and block_type not in {"tool_call", "tool_call_chunk"}:
            append_stream_block(blocks, block)
    return blocks


__all__ = ["append_stream_block", "clean_stream_block", "stream_content_blocks"]
