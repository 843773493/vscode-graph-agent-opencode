"""provider content 的历史/详情纯映射。

输入必须已经越过 provider response normalization 边界；本模块不读写存储、
不调用 provider SDK，也不生成 canonical item。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.mapping.itemized.content_blocks import (
    direct_content_blocks,
    reasoning_content_rows,
)


def visible_text(content: Any) -> str:
    """从直接 content blocks 提取可见文本，不读取思考块。"""
    parts: list[str] = []
    for block in direct_content_blocks(content):
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


def reasoning_projection_rows(content: Any) -> list[dict[str, object]]:
    """合并不同 provider reasoning carrier，保持最早 block 坐标。"""
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

    for row in reasoning_content_rows(content):
        append_row(
            block_index=row.content_block_index,
            item_index=row.item_index,
            carrier_type="reasoning_items" if row.carrier_type == "reasoning" else row.carrier_type,
            item_id=row.item_id,
            reasoning_text=row.reasoning_text,
            summary_text=row.summary_text,
            encrypted=row.encrypted,
            signature_present=row.signature_present,
        )

    identified_keys = {
        row["item_id"]
        for row in rows
        if isinstance(row.get("item_id"), str) and row["item_id"]
    }
    single_item_key = (
        next(iter(identified_keys), None) if len(identified_keys) == 1 else None
    )
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
        key = (
            ("provider_item", item_id)
            if isinstance(item_id, str) and item_id
            else ("provider_item", single_item_key)
            if single_item_key is not None
            else ("message_reasoning", "anonymous")
        )
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
            target.get("reasoning_text"), row.get("reasoning_text")
        ) or None
        target["summary_text"] = merge_text(
            target.get("summary_text"), row.get("summary_text")
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


__all__ = ["reasoning_projection_rows", "visible_text"]
