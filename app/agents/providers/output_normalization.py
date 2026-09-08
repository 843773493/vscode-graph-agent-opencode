"""Provider 响应内容的无状态 block 归一化。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from app.agents.providers.message_content_schema import validate_content_blocks

MISSING = object()


def as_block(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def content_blocks(content: Any) -> list[dict[str, Any]]:
    """把 provider response content 归一化为有序 block。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            if value:
                blocks.append({"type": "text", "text": value})
            continue
        block = as_block(value)
        if block is not None:
            blocks.append(block)
        elif value is not None:
            blocks.append({"type": "text", "text": str(value)})
    return blocks


def append_unique(blocks: list[dict[str, Any]], block: dict[str, Any]) -> None:
    if block not in blocks:
        blocks.append(copy.deepcopy(block))


def reasoning_content_block(text: str) -> dict[str, Any]:
    return {"type": "reasoning_content", "reasoning_content": text}


def reasoning_items_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "reasoning_items", "reasoning_items": copy.deepcopy(items)}


def build_ai_message_content(
    content: Any,
    *,
    source_provider: str | None = None,
    source_model: str | None = None,
    reasoning_content: Any = MISSING,
    thinking_blocks: Any = MISSING,
    reasoning_items: Any = MISSING,
) -> Any:
    """把 provider 响应组装成有序、可校验的直接 content blocks。

    该函数只处理 provider response 的 carrier 归一化；LangChain message
    的生命周期和 canonical item 仍由上层 mapping/domain owner 负责。
    ``source_provider``/``source_model`` 只属于调用侧审计，不能进入正文。
    """
    del source_provider, source_model
    direct_blocks = content_blocks(content)
    direct_types = {block.get("type") for block in direct_blocks}
    blocks: list[dict[str, Any]] = []

    if (
        isinstance(reasoning_content, str)
        and reasoning_content
        and "reasoning_content" not in direct_types
    ):
        blocks.append(reasoning_content_block(reasoning_content))

    if isinstance(thinking_blocks, list) and not (
        {"thinking", "redacted_thinking"} & direct_types
    ):
        for value in thinking_blocks:
            block = as_block(value)
            if block is not None and block.get("type") in {
                "thinking",
                "redacted_thinking",
            }:
                blocks.append(block)

    if isinstance(reasoning_items, list) and "reasoning_items" not in direct_types:
        items = [
            block
            for value in reasoning_items
            if (block := as_block(value)) is not None
        ]
        if items:
            blocks.append(reasoning_items_block(items))

    blocks.extend(copy.deepcopy(block) for block in direct_blocks)
    if not blocks:
        return ""
    return validate_content_blocks(blocks)


__all__ = [
    "MISSING",
    "append_unique",
    "as_block",
    "build_ai_message_content",
    "content_blocks",
    "reasoning_content_block",
    "reasoning_items_block",
]
