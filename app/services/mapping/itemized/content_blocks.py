"""已接受消息内容的无 I/O block view。

这里遍历已有消息并映射单块语义 payload，不执行 provider 响应归一化
或外部请求编码。provider 响应的归一化仍归
``app.agents.providers``。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.itemized.identity.item_projection import ReasoningCarrier
from app.services.mapping.agent_content_mapper import extract_reasoning_summary

REASONING_CARRIER_TYPES = frozenset({
    "reasoning", "reasoning_content", "reasoning_items", "thinking", "redacted_thinking",
})


def semantic_content_payload(
    part: object,
) -> tuple[str, str, object, ReasoningCarrier | None]:
    """将一个已接受内容块映射为语义 payload，流式与 checkpoint 共用。"""
    kind = "assistant_output"
    payload_kind = "text" if isinstance(part, str) else "structured_content"
    payload = part if isinstance(part, str) else [part]
    carrier: dict[str, object] = {}
    if isinstance(part, Mapping) and part.get("type") in REASONING_CARRIER_TYPES:
        kind = "reasoning"
        text_key = next(
            (
                key
                for key in ("reasoning", "reasoning_content", "text", "thinking")
                if isinstance(part.get(key), str)
            ),
            None,
        )
        if text_key is not None and set(part) <= {"type", text_key, "id", "index"}:
            payload_kind, payload = "text", part[text_key]
            carrier = {key: value for key, value in part.items() if key != text_key}
            carrier["text_key"] = text_key
        else:
            # 有保护字段的 carrier 使用 domain 允许的 extension envelope。
            payload_kind = "extension"
            payload = {
                "extension_schema": "boxteam.checkpoint.reasoning-carrier",
                "extension_version": "1",
                "encoding": "json",
                "wire_type": str(part["type"]),
                "schema_version": "1",
                "value": dict(part),
                "protection": {"visibility": "protected"},
            }
    elif not isinstance(part, (str, Mapping)):
        raise TypeError("assistant content block 必须是字符串或 object")
    return kind, payload_kind, payload, ReasoningCarrier.from_dict(carrier) if carrier else None


def has_assistant_output(content: Sequence[Mapping[str, object]]) -> bool:
    """判断已接受内容中是否有输出；媒体不要求同时携带文本。"""
    for block in content:
        block_type = block.get("type")
        if block_type in REASONING_CARRIER_TYPES:
            continue
        if block_type in {"text", "output_text"}:
            if block.get("text"):
                return True
        elif block_type == "refusal":
            if block.get("refusal"):
                return True
        else:
            return True
    return False


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


def iter_direct_content_blocks(content: Any) -> Iterator[dict[str, Any]]:
    """只读遍历 content；输出方按实际保留字段复制，避免先复制整份嵌套载荷。"""
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

    for block_index, block in enumerate(iter_direct_content_blocks(content)):
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


__all__ = ["REASONING_CARRIER_TYPES", "ReasoningContentRow", "has_assistant_output", "iter_direct_content_blocks", "reasoning_content_rows", "reasoning_content_text"]
