"""v2 checkpoint message materialization and view-chain validation owner。

本模块只从已经提交的 v2 item/catalog/view 读取并校验，不推断 v1 物理
邻接，也不把 LangChain message 作为持久化事实。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import BinaryIO

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_projection,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line as _v2_json_line,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutCanonicalReadMixin:
    @staticmethod
    def _read_v2_item_at(
        connection: sqlite3.Connection,
        stream: BinaryIO,
        *,
        sequence: int,
        offset: int,
        length: int,
    ) -> CanonicalItemRecord:
        """从 catalog 指向的 JSONL line 读取并校验唯一 canonical item。

        ``messages`` 只是 LangGraph 的投影定位表。任何 history/checkpoint
        恢复都必须先通过它定位 item，再从 JSONL/catalog 校验 identity、hash
        和不可变 envelope；envelope 不允许包含 message projection。
        """
        sequence = strict_non_negative_int(sequence, field="v2 item sequence")
        offset = strict_non_negative_int(offset, field="v2 item offset")
        length = strict_non_negative_int(length, field="v2 item length")
        if length == 0:
            raise RuntimeError(
                f"v2 item locator 非法: sequence={sequence}, offset={offset}, length={length}"
            )
        stream.seek(offset)
        raw = stream.read(length)
        if len(raw) != length:
            raise RuntimeError(f"v2 item locator 超出 JSONL: sequence={sequence}")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"v2 item envelope 无法解码: sequence={sequence}"
            ) from error
        if not isinstance(envelope, Mapping):
            raise TypeError(f"v2 item envelope 非 object: sequence={sequence}")
        if raw != _v2_json_line(envelope):
            raise RuntimeError(
                f"v2 item envelope 不是 canonical JCS line: sequence={sequence}"
            )
        item = CanonicalItemRecord.from_dict(envelope)
        catalog = connection.execute(
            "SELECT item_sequence, content_hash, jsonl_offset, jsonl_length, projection_manifest_json, tool_call_id FROM item_catalog WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
        if catalog is None:
            raise RuntimeError(f"v2 item 缺少 item_catalog: {item.item_id}")
        catalog_sequence = strict_non_negative_int(
            catalog[0], field=f"item_catalog.item_sequence: {item.item_id}"
        )
        catalog_offset = strict_non_negative_int(
            catalog[2], field=f"item_catalog.jsonl_offset: {item.item_id}"
        )
        catalog_length = strict_non_negative_int(
            catalog[3], field=f"item_catalog.jsonl_length: {item.item_id}"
        )
        if catalog_length == 0:
            raise RuntimeError(
                f"item_catalog.jsonl_length 必须大于 0: {item.item_id}"
            )
        if (
            catalog_sequence != sequence
            or strict_text(catalog[1], field="item_catalog.content_hash")
            != item.content_hash
            or catalog_offset != offset
            or catalog_length != length
        ):
            raise RuntimeError(
                f"v2 item catalog 与 JSONL locator/hash 不一致: {item.item_id}"
            )
        relations = connection.execute(
            "SELECT canonical_relation_ordinal, relation, source_ref, target_ref "
            "FROM item_relations WHERE canonical_item_id = ? ORDER BY canonical_relation_ordinal",
            (item.item_id,),
        ).fetchall()
        validate_catalog_projection(
            item, projection_manifest_json=strict_text(catalog[4], field="item_catalog.projection_manifest_json"),
            tool_call_id=strict_optional_text(catalog[5], field="item_catalog.tool_call_id"),
            relation_rows=relations,
        )
        return item

    def _read_canonical_item_from_catalog(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        item_id: str,
    ) -> CanonicalItemRecord:
        """在同一写事务中恢复一个已提交 catalog item，供 projection 补齐。"""
        row = connection.execute(
            "SELECT item_sequence, content_hash, jsonl_offset, jsonl_length "
            "FROM item_catalog WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"canonical item 不存在: {item_id}")
        sequence = strict_non_negative_int(
            row[0], field="item_catalog.item_sequence"
        )
        expected_hash = strict_text(
            row[1], field="item_catalog.content_hash"
        )
        offset = strict_non_negative_int(row[2], field="item_catalog.jsonl_offset")
        length = strict_non_negative_int(row[3], field="item_catalog.jsonl_length")
        if length == 0:
            raise RuntimeError(f"canonical item JSONL locator 长度非法: {item_id}")
        jsonl_path = self.jsonl_path(
            session_id,
            checkpoint_ns,
            thread_id=thread_id,
        )
        with jsonl_path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(length)
        if len(raw) != length:
            raise RuntimeError(f"canonical item JSONL locator 长度不足: {item_id}")
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, Mapping):
            raise TypeError(f"canonical item JSONL envelope 非法: {item_id}")
        if raw != canonical_json_line(envelope):
            raise RuntimeError(f"canonical item JSONL 不是 canonical line: {item_id}")
        item = CanonicalItemRecord.from_dict(envelope)
        if (
            item.item_id != item_id
            or item.item_sequence != sequence
            or item.content_hash != expected_hash
        ):
            raise RuntimeError(f"canonical item catalog 与 JSONL identity 冲突: {item_id}")
        return item
