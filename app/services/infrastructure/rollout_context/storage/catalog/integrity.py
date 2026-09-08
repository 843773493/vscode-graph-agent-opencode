"""校验 canonical JSONL 与 catalog 的 manifest，不执行回填或修复。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)


def validate_catalog_body(
    raw: bytes,
    *,
    sequence: int,
    item_id: str,
    catalog_hash: str,
    payload_length: int,
    source_revision: str,
) -> None:
    """以已提交正文验证 locator、hash 和逻辑 source manifest。"""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"v2 item JSONL 无法解码: item_id={item_id}") from error
    if not isinstance(envelope, Mapping):
        raise TypeError(f"v2 item envelope 非 object: item_id={item_id}")
    if raw != canonical_json_line(envelope):
        raise RuntimeError(f"v2 item JSONL 不是 canonical JCS line: item_id={item_id}")
    if (
        envelope.get("format_version") != 2
        or envelope.get("record_type") != "item"
        or envelope.get("item_sequence") != sequence
        or envelope.get("item_id") != item_id
        or envelope.get("content_hash") != catalog_hash
    ):
        raise RuntimeError(f"v2 item catalog 与 JSONL identity 不一致: item_id={item_id}")
    try:
        item = CanonicalItemRecord.from_dict(envelope)
    except (KeyError, ItemSchemaError, TypeError, ValueError) as error:
        raise RuntimeError(f"v2 item JSONL schema/content 非法: item_id={item_id}") from error
    if payload_length != payload_content_length(item.payload_kind, item.payload):
        raise RuntimeError(f"item catalog 与 JSONL payload_length 不一致: {item_id}")
    expected_revision = item.metadata.get("source_revision")
    if expected_revision is None:
        expected_revision = f"canonical:{item.item_id}:{item.content_hash}"
    if source_revision != expected_revision:
        raise RuntimeError(f"item catalog 与 JSONL source_revision 不一致: {item_id}")


def validate_projection_membership(connection: sqlite3.Connection) -> None:
    """已有 projection 必须有对应 catalog；最小 catalog 不强制重型索引。"""
    orphan = connection.execute(
        "SELECT p.item_id FROM item_projections p LEFT JOIN item_catalog c "
        "ON c.item_id = p.item_id AND c.item_sequence = p.item_sequence "
        "WHERE c.item_id IS NULL LIMIT 1"
    ).fetchone()
    if orphan is not None:
        raise RuntimeError(
            f"item projection 与 canonical catalog membership 不一致: "
            f"orphan={orphan}"
        )
