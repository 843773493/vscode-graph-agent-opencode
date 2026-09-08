"""消息 locator 到完整 canonical group 的受校验读取，不进行消息投影。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import BinaryIO

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)


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
    manifest = anchor.metadata.get("projection_group")
    if manifest is None:
        return (anchor,)
    if not isinstance(manifest, Mapping) or not anchor.message_group_id:
        raise RuntimeError("canonical projection group manifest 非法")
    rows = connection.execute(
        "SELECT item_sequence, jsonl_offset, jsonl_length FROM item_catalog "
        "WHERE message_group_id = ? ORDER BY item_sequence",
        (anchor.message_group_id,),
    ).fetchall()
    if type(manifest.get("size")) is not int or len(rows) != manifest["size"]:
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
