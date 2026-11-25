"""用户授权的 rollout v29→v30：从已提交消息补算活动轮廓，原始事实不变。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.mapping.itemized.content_blocks import (
    iter_direct_content_blocks,
    reasoning_content_rows,
)
from app.services.mapping.itemized.provider_history import visible_text


def response_outlines_v30(items: Sequence[CanonicalItemRecord]) -> dict[str, list[dict[str, object]]]:
    """冻结已授权29→30的Part轮廓；现行writer／reader不使用此旧合同。

    TODO：仅适用于用户授权的SQLite29→30来源；支持范围正式退出后随原迁移删除。
    不能调用现行Item展示mapper，否则未来DTO与语义粒度变化会改变历史迁移结果。
    """
    result: dict[str, list[dict[str, object]]] = {}
    for item in items:
        if item.semantic_kind not in {"assistant_output", "reasoning", "tool_call", "tool_result", "compaction_summary"}:
            continue
        identity, payload = item.projection_identity, item.payload
        ordinal = identity.block_index if identity and identity.block_index is not None else 0
        block_index = identity.block_index if identity else None
        rows: list[dict[str, object]] = []
        if item.semantic_kind == "assistant_output" and item.payload_kind == "structured_content":
            for index, block in enumerate(iter_direct_content_blocks(payload)):
                if block.get("type") in {"text", "input_text", "output_text", "refusal"}:
                    rows.append({"kind": "text", "content_block_index": index,
                        "item_index": None, "carrier_type": None, "text": visible_text([block])})
            for row in reasoning_content_rows(payload):
                for kind, value in (("reasoning", row.reasoning_text), ("reasoning_summary", row.summary_text),
                                    ("reasoning_encrypted", "" if row.encrypted else None)):
                    if value or kind == "reasoning_encrypted" and value is not None:
                        rows.append({"kind": kind, "content_block_index": row.content_block_index,
                            "item_index": row.item_index, "carrier_type": row.carrier_type, "text": ""})
            rows.sort(key=lambda row: (row["content_block_index"], row["item_index"] or 0))
            for index, row in enumerate(rows):
                row["part_ordinal"] = index
        elif item.semantic_kind == "tool_call":
            calls = payload.get("tool_calls", [payload]) if isinstance(payload, Mapping) else [payload]
            for index, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    raise TypeError(f"v29 tool_call payload 非法: {item.item_id}")
                rows.append({"kind": "tool_call", "part_ordinal": ordinal + index,
                    "call_index": index, "tool_call_id": call.get("id", call.get("tool_call_id")),
                    "tool_name": call.get("name"), "text": ""})
        else:
            kind = "text" if item.semantic_kind == "assistant_output" else item.semantic_kind
            if kind == "reasoning":
                kind = "reasoning_summary" if item.payload_kind == "summary" else (
                    "reasoning_encrypted" if item.payload_kind in {"opaque", "extension"} else "reasoning")
            row = {"kind": kind, "part_ordinal": ordinal, "text": payload if kind == "text" else "",
                "carrier_type": "compaction_summary" if kind == "compaction_summary" else None}
            if kind == "tool_result":
                row.update(tool_call_id=item.tool_result_evidence.tool_call_id if item.tool_result_evidence else payload["tool_call_id"],
                    tool_name=payload.get("name") if isinstance(payload, Mapping) else None)
            rows.append(row)
        remaining = 65536
        outline = []
        for row in rows:
            value = row["text"]
            if not isinstance(value, str):
                raise TypeError(f"v29活动文本非法: {item.item_id}")
            text = value[:remaining] if row["kind"] == "text" else ""
            remaining -= len(text)
            outline.append({"kind": row["kind"], "part_ordinal": row["part_ordinal"],
                "content_block_index": row.get("content_block_index", block_index),
                "item_index": row.get("item_index"), "carrier_type": row.get("carrier_type"),
                "call_index": row.get("call_index"), "tool_call_id": row.get("tool_call_id"),
                "tool_name": row.get("tool_name"), "text": text,
                "truncated": row["kind"] == "text" and len(text) < len(value)})
        result[item.item_id] = outline
    return result


def migrate_response_outlines(
    connection: sqlite3.Connection, jsonl_path: Path,
) -> None:
    """沿已有 locator 逐条读取，生成冻结的v30投影；不物化整份历史。"""
    rows = connection.execute(
        "SELECT projection.message_sequence,projection.text_preview,projection.visible_text,"
        "projection.visible_text_length,projection.visible_text_truncated,projection.has_reasoning,"
        "projection.has_encrypted_reasoning,projection.has_tool_calls,projection.phase,"
        "projection.projection_version,projection.updated_at,message.jsonl_offset,message.jsonl_length "
        "FROM message_projections_v29 projection LEFT JOIN messages message "
        "ON message.message_sequence=projection.message_sequence ORDER BY projection.message_sequence"
    )
    with jsonl_path.open("rb") as stream:
        for row in rows:
            items = read_message_group(connection, stream, offset=row[11], length=row[12],
                read_item=RolloutCanonicalReadMixin._read_item_at)
            connection.execute(
                "INSERT INTO message_projections(message_sequence,text_preview,visible_text,"
                "visible_text_length,visible_text_truncated,has_reasoning,has_encrypted_reasoning,"
                "has_tool_calls,phase,projection_version,updated_at,response_outline_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (*row[:11], canonical_json_text(response_outlines_v30(items))),
            )
