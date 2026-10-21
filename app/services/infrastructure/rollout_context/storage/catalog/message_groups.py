"""消息 locator 到完整 canonical group 的受校验读取，不进行消息投影。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from typing import BinaryIO

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)

MessageLocator = tuple[int, str | None, str, int, int]


def find_message_locator(
    connection: sqlite3.Connection, message_id: str, *,
    item_id: str | None = None, sequences: Sequence[int] | None = None,
) -> MessageLocator | None:
    """logical ID 只用于校验；已知 item/view 时必须按其冻结坐标解析版本。"""
    rows = connection.execute(
        "SELECT message.message_sequence,message.turn_id,message.execution_id,"
        "message.jsonl_offset,message.jsonl_length,item.item_id "
        "FROM messages message LEFT JOIN item_catalog item "
        "ON item.jsonl_offset=message.jsonl_offset AND item.jsonl_length=message.jsonl_length "
        "WHERE message.message_id=?" + (" AND item.item_id=?" if item_id is not None else ""),
        (message_id, item_id) if item_id is not None else (message_id,),
    ).fetchall()
    if sequences is not None:
        allowed = set(sequences)
        rows = [row for row in rows if row[0] in allowed]
    if len(rows) > 1:
        raise RuntimeError(f"message locator 版本歧义，必须提供精确 canonical item/view: {message_id}")
    if rows and rows[0][5] is None:
        raise RuntimeError(f"message projection 缺少 canonical item: {message_id}")
    if not rows:
        if item_id is not None:
            raise RuntimeError(f"message locator 缺少指定 canonical item: {message_id}/{item_id}")
        return None
    return tuple(rows[0][:5])


def read_message_group(
    connection: sqlite3.Connection, stream: BinaryIO, *,
    offset: int, length: int,
    read_item: Callable[..., CanonicalItemRecord],
) -> tuple[CanonicalItemRecord, ...]:
    """message_sequence 与 item_sequence 独立；先解析 anchor 的真实坐标。"""
    anchors = connection.execute(
        "SELECT item_sequence FROM item_catalog WHERE jsonl_offset = ? AND jsonl_length = ?",
        (offset, length),
    ).fetchall()
    if len(anchors) != 1:
        raise RuntimeError("message locator 必须恰好指向一个 canonical item")
    anchor = read_item(
        connection, stream,
        sequence=strict_non_negative_int(anchors[0][0], field="item_catalog.item_sequence"),
        offset=offset, length=length,
    )
    manifest = anchor.projection_group
    if manifest is None:
        return (anchor,)
    if not anchor.message_group_id:
        raise RuntimeError("canonical projection group manifest 非法")
    rows = connection.execute(
        "SELECT item_sequence, jsonl_offset, jsonl_length FROM item_catalog "
        "WHERE message_group_id = ? ORDER BY item_sequence",
        (anchor.message_group_id,),
    ).fetchall()
    if len(rows) != manifest.size:
        raise RuntimeError("canonical projection group 缺少或多出成员")
    items = tuple(
        anchor if row[0] == anchor.item_sequence else read_item(
            connection, stream,
            sequence=strict_non_negative_int(row[0], field="item_catalog.item_sequence"),
            offset=strict_non_negative_int(row[1], field="item_catalog.jsonl_offset"),
            length=strict_non_negative_int(row[2], field="item_catalog.jsonl_length"),
        )
        for row in rows
    )
    if items[-1].item_id != anchor.item_id:
        raise RuntimeError("message locator 没有指向 canonical group anchor")
    return items
