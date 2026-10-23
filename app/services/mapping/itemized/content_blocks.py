"""已接受消息内容的无 I/O block view。

这里仅把已有消息内容展开为可遍历的 block，不执行 provider 响应归一化、
schema 选择或外部请求编码。provider 响应的归一化仍归
``app.agents.providers``。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.mapping.agent_content_mapper import extract_reasoning_summary


@dataclass(frozen=True, slots=True)
class ReasoningContentRow:
    """单个 canonical reasoning 来源，保留原文与 block/inner item 坐标。"""

    content_block_index: int
    item_index: int
    carrier_type: str
    item_id: str | None = None
    reasoning_text: str | None = None
    summary_text: str | None = None
    encrypted: str | None = None
    signature_present: bool = False


def _iter_direct_content_blocks(content: Any) -> Iterator[dict[str, Any]]:
    """展开 content block；只规范化外层键，不复制嵌套载荷。"""
    if isinstance(content, Mapping) and "attachments" in content and "content" in content:
        content = content["content"]
    if content is None:
        return
    if isinstance(content, str):
        if content:
            yield {"type": "text", "text": content}
        return
    if not isinstance(content, list):
        yield {"type": "text", "text": str(content)}
        return

    for value in content:
        if isinstance(value, str):
            if value:
                yield {"type": "text", "text": value}
            continue
        if isinstance(value, Mapping):
            yield {str(key): item for key, item in value.items()}
            continue
        if value is not None:
            yield {"type": "text", "text": str(value)}


def direct_content_blocks(content: Any) -> list[dict[str, Any]]:
    """将已接受的消息 content 展开为独立副本，保持原有顺序。"""
    return [
        {key: copy.deepcopy(value) for key, value in block.items()}
        for block in _iter_direct_content_blocks(content)
    ]


def _reasoning_content_text(block: Mapping[str, object]) -> str:
    """读取 reasoning 正文原文，不清理空白或合并不同来源。"""
    direct = block.get("reasoning")
    if isinstance(direct, str):
        return direct
    content = block.get("content")
    if isinstance(content, list):
        return "".join(
            str(item.get("text"))
            for item in content
            if isinstance(item, Mapping)
            and item.get("type") in {"reasoning_text", "text"}
            and isinstance(item.get("text"), str)
        )
    return ""


def reasoning_content_text(block: Mapping[str, Any]) -> str:
    """读取 provider 投影正文，沿用去除首尾空白的既有契约。"""
    return _reasoning_content_text(block).strip()


def reasoning_content_rows(content: object) -> list[ReasoningContentRow]:
    """逐 block 与 inner item 无损拆解；不按正文或 provider item 身份折叠。"""
    rows: list[ReasoningContentRow] = []

    def append_item(
        block_index: int, item_index: int, carrier_type: str,
        item: Mapping[str, object],
    ) -> None:
        item_id = item.get("id")
        encrypted = item.get("encrypted_content")
        rows.append(ReasoningContentRow(
            content_block_index=block_index, item_index=item_index,
            carrier_type=carrier_type,
            item_id=item_id if isinstance(item_id, str) else None,
            reasoning_text=_reasoning_content_text(item),
            summary_text=extract_reasoning_summary(item.get("summary")),
            encrypted=encrypted if isinstance(encrypted, str) else None,
        ))

    for block_index, block in enumerate(_iter_direct_content_blocks(content)):
        block_type = block.get("type")
        if block_type == "reasoning_items":
            items = block.get("reasoning_items")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    if isinstance(item, Mapping):
                        append_item(block_index, item_index, block_type, item)
        elif block_type == "reasoning":
            append_item(block_index, 0, block_type, block)
        elif block_type in {"reasoning_content", "thinking", "redacted_thinking"}:
            value = None
            if block_type == "thinking":
                value = block.get("thinking") or block.get("text")
            elif block_type == "reasoning_content":
                value = block.get("reasoning_content")
            encrypted = block.get("data") if block_type == "redacted_thinking" else None
            rows.append(ReasoningContentRow(
                content_block_index=block_index, item_index=0,
                carrier_type=block_type,
                reasoning_text=value if isinstance(value, str) else None,
                encrypted=encrypted if isinstance(encrypted, str) else None,
                signature_present=(
                    block_type == "thinking"
                    and isinstance(block.get("signature"), str)
                    and bool(block.get("signature"))
                ),
            ))
    return rows


__all__ = ["ReasoningContentRow", "direct_content_blocks", "reasoning_content_rows", "reasoning_content_text"]
