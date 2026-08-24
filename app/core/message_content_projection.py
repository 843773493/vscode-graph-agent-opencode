"""对 canonical AIMessage 内容执行与 provider 无关的 SQLite 投影。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    result: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            if value:
                result.append({"type": "text", "text": value})
        elif isinstance(value, Mapping):
            result.append({str(key): item for key, item in value.items()})
        elif value is not None:
            result.append({"type": "text", "text": str(value)})
    return result


def visible_text(content: Any) -> str:
    """提取可见正文，不读取 reasoning 或 encrypted thinking。"""
    parts: list[str] = []
    for block in _blocks(content):
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "refusal":
            refusal = block.get("refusal")
            if isinstance(refusal, str):
                parts.append(f"[拒绝]{refusal}")
    return "".join(parts)


def _summary_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return str(value).strip()
    return "".join(
        item if isinstance(item, str) else item.get("text", "")
        for item in value
        if isinstance(item, str)
        or isinstance(item, Mapping) and isinstance(item.get("text"), str)
    ).strip()


def _reasoning_content_text(block: Mapping[str, Any]) -> str:
    direct = block.get("reasoning")
    if isinstance(direct, str):
        return direct.strip()
    content = block.get("content")
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, Mapping)
            and item.get("type") in {"reasoning_text", "text"}
            and isinstance(item.get("text"), str)
        ).strip()
    return ""


def reasoning_projection_rows(content: Any) -> list[dict[str, object]]:
    """返回带 content 坐标的 reasoning 投影，不返回 encrypted 正文。"""
    rows: list[dict[str, object]] = []

    def append_row(
        *,
        block_index: int,
        item_index: int,
        carrier_type: str,
        item_id: str | None = None,
        reasoning_text: str | None = None,
        summary_text: str | None = None,
        encrypted: str | None = None,
        signature_present: bool = False,
    ) -> None:
        clean_reasoning = reasoning_text.strip() if reasoning_text else ""
        clean_summary = summary_text.strip() if summary_text else ""
        has_encrypted = bool(encrypted)
        if not clean_reasoning and not clean_summary and not has_encrypted:
            return
        kind = (
            "encrypted"
            if has_encrypted and not (clean_reasoning or clean_summary)
            else "reasoning"
            if clean_reasoning
            else "summary"
        )
        row: dict[str, object] = {
            "kind": kind,
            "text": clean_reasoning or clean_summary,
            "reasoning_text": clean_reasoning or None,
            "summary_text": clean_summary or None,
            "content_block_index": block_index,
            "item_index": item_index,
            "carrier_type": carrier_type,
            "signature_present": signature_present,
        }
        if item_id:
            row["item_id"] = item_id
        if has_encrypted and encrypted is not None:
            row["encrypted_length"] = len(encrypted)
            row["encrypted_hash"] = hashlib.sha256(
                encrypted.encode("utf-8")
            ).hexdigest()
        rows.append(row)

    for block_index, block in enumerate(_blocks(content)):
        block_type = block.get("type")
        if block_type == "reasoning_content":
            value = block.get("reasoning_content")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="reasoning_content",
                reasoning_text=value if isinstance(value, str) else None,
            )
        elif block_type == "reasoning_items":
            items = block.get("reasoning_items")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    if isinstance(item, Mapping):
                        append_row(
                            block_index=block_index,
                            item_index=item_index,
                            carrier_type="reasoning_items",
                            item_id=(
                                item.get("id")
                                if isinstance(item.get("id"), str)
                                else None
                            ),
                            reasoning_text=_reasoning_content_text(item),
                            summary_text=_summary_text(item.get("summary")),
                            encrypted=(
                                item.get("encrypted_content")
                                if isinstance(item.get("encrypted_content"), str)
                                else None
                            ),
                        )
        elif block_type == "reasoning":
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="reasoning_items",
                item_id=block.get("id") if isinstance(block.get("id"), str) else None,
                reasoning_text=_reasoning_content_text(block),
                summary_text=_summary_text(block.get("summary")),
                encrypted=(
                    block.get("encrypted_content")
                    if isinstance(block.get("encrypted_content"), str)
                    else None
                ),
            )
        elif block_type == "thinking":
            value = block.get("thinking") or block.get("text")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="thinking",
                reasoning_text=value if isinstance(value, str) else None,
                signature_present=(
                    isinstance(block.get("signature"), str)
                    and bool(block.get("signature"))
                ),
            )
        elif block_type == "redacted_thinking":
            encrypted = block.get("data")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="redacted_thinking",
                encrypted=encrypted if isinstance(encrypted, str) else None,
            )
    return rows
