"""用户授权的 rollout v29→v30：从已提交消息补算活动轮廓，原始事实不变。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


def migrate_response_outlines(
    connection: sqlite3.Connection, jsonl_path: Path, codec: MessageCodec,
) -> None:
    """沿已有 locator 逐条读取，复用 writer 的 codec；不物化整份历史。"""
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
                read_item=RolloutCanonicalReadMixin._read_v2_item_at)
            connection.execute(
                "INSERT INTO message_projections(message_sequence,text_preview,visible_text,"
                "visible_text_length,visible_text_truncated,has_reasoning,has_encrypted_reasoning,"
                "has_tool_calls,phase,projection_version,updated_at,response_outline_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (*row[:11], canonical_json_text(codec.activity_outline(items))),
            )
