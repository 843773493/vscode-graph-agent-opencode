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
    """返回按逻辑 reasoning item 合并的投影，不返回 encrypted 正文。

    provider 可能会同时通过 reasoning_content、reasoning_items 和
    redacted_thinking 表达同一次思考。这里先保留最早来源坐标，再按 provider
    item id 合并，避免历史详情把同一个思考渲染成三段正文。
    """
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
    identified_keys = {
        row["item_id"]
        for row in rows
        if isinstance(row.get("item_id"), str) and row["item_id"]
    }
    single_item_key = next(iter(identified_keys), None) if len(identified_keys) == 1 else None
    merged: list[dict[str, object]] = []
    merged_indexes: dict[tuple[str, object], int] = {}

    def merge_text(previous: object, current: object) -> str:
        previous_text = previous if isinstance(previous, str) else ""
        current_text = current if isinstance(current, str) else ""
        if not previous_text:
            return current_text
        if not current_text or current_text == previous_text:
            return previous_text
        if current_text.startswith(previous_text):
            return current_text
        if previous_text.startswith(current_text):
            return previous_text
        return previous_text + current_text

    for row in rows:
        item_id = row.get("item_id")
        block_index = row.get("content_block_index")
        if isinstance(item_id, str) and item_id:
            key: tuple[str, object] = ("provider_item", item_id)
        elif single_item_key is not None:
            key = ("provider_item", single_item_key)
        else:
            key = ("message_reasoning", "anonymous")

        existing_index = merged_indexes.get(key)
        if existing_index is None:
            merged_indexes[key] = len(merged)
            merged.append(dict(row))
            continue

        target = merged[existing_index]
        if "item_id" not in target and isinstance(item_id, str) and item_id:
            target["item_id"] = item_id
        previous_carriers = target.get("carrier_type")
        current_carrier = row.get("carrier_type")
        if (
            isinstance(previous_carriers, str)
            and isinstance(current_carrier, str)
            and current_carrier
            and current_carrier not in previous_carriers.split("+")
        ):
            target["carrier_type"] = f"{previous_carriers}+{current_carrier}"[:64]
        target["reasoning_text"] = merge_text(
            target.get("reasoning_text"),
            row.get("reasoning_text"),
        ) or None
        target["summary_text"] = merge_text(
            target.get("summary_text"),
            row.get("summary_text"),
        ) or None
        if row.get("encrypted_length") is not None:
            previous_length = target.get("encrypted_length")
            current_length = row.get("encrypted_length")
            if isinstance(current_length, int):
                target["encrypted_length"] = (
                    (previous_length if isinstance(previous_length, int) else 0)
                    + current_length
                )
            if "encrypted_hash" not in target and row.get("encrypted_hash") is not None:
                target["encrypted_hash"] = row["encrypted_hash"]
        target["signature_present"] = bool(
            target.get("signature_present") or row.get("signature_present")
        )
        reasoning_text = target.get("reasoning_text")
        summary_text = target.get("summary_text")
        target["kind"] = (
            "reasoning"
            if isinstance(reasoning_text, str) and reasoning_text
            else "summary"
            if isinstance(summary_text, str) and summary_text
            else "encrypted"
        )
        target["text"] = (
            reasoning_text
            if isinstance(reasoning_text, str) and reasoning_text
            else summary_text
            if isinstance(summary_text, str) and summary_text
            else ""
        )

    return merged
