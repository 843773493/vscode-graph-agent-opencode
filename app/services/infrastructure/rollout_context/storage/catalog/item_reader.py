"""v2 checkpoint message materialization and view-chain validation owner。

本模块只从已经提交的 v2 item/catalog/view 读取并校验，不推断 v1 物理
邻接，也不把 LangChain message 作为持久化事实。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import BinaryIO

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    read_catalog_body,
    validate_catalog_projection,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    STORED_COMMIT_COLUMNS,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
    validate_stored_commit,
)

# 与正文 locator 同次查询提交和 owner；按 item 主键选集做索引连接，不扫描未消费历史。
CATALOG_READ_COLUMNS = (
    STORED_COMMIT_COLUMNS
    + ",execution.session_id,execution.thread_id,turn.session_id,turn.thread_id,"
    "meta.session_id,meta.thread_id,meta.committed_jsonl_offset"
)
CATALOG_READ_JOINS = (
    " FROM item_catalog item "
    "LEFT JOIN storage_commits commit_record ON commit_record.commit_id=item.commit_id "
    "LEFT JOIN executions execution ON execution.execution_id=item.execution_id "
    "LEFT JOIN turn_records turn ON turn.turn_id=item.turn_id "
    "JOIN database_meta meta ON meta.singleton_id=1 "
)


def validate_catalog_read_boundary(
    connection: sqlite3.Connection, boundary: Sequence[object], *,
    item_id: str, offset: int, length: int,
    validated_commits: set[int], validated_scopes: set[tuple[str, str]],
) -> None:
    """只核实际消费 item 的提交与历史授权；校验结果仅在本次读取内共用。"""
    commit_id = strict_non_negative_int(boundary[0], field=f"item_catalog storage commit: {item_id}")
    if commit_id not in validated_commits:
        validate_stored_commit(boundary[:12])
        validated_commits.add(commit_id)
    start = strict_non_negative_int(boundary[1], field="storage commit jsonl_start_offset")
    end = strict_non_negative_int(boundary[2], field="storage commit jsonl_end_offset")
    committed = strict_non_negative_int(boundary[18], field="database_meta.committed_jsonl_offset")
    if not start <= offset < offset + length <= end <= committed:
        raise RuntimeError(f"storage commit 的 item catalog offset 不在已提交边界内: item_id={item_id}")
    execution_scope = tuple(boundary[12:14]) if boundary[12] is not None else None
    turn_scope = tuple(boundary[14:16]) if boundary[14] is not None else None
    if execution_scope is not None and turn_scope is not None and execution_scope != turn_scope:
        raise ValueError(f"source-mismatch: canonical item 的 Turn/execution 历史 owner 不一致: {item_id}")
    owner = (strict_text(boundary[16], field="database_meta.session_id"),
             strict_text(boundary[17], field="database_meta.thread_id"))
    scope = execution_scope or turn_scope or owner
    item_owner = (strict_text(scope[0], field="item owner session_id"),
                  strict_text(scope[1], field="item owner thread_id"))
    if item_owner not in validated_scopes:
        require_historical_scope(connection, *item_owner, current_owner=owner)
        validated_scopes.add(item_owner)


class RolloutCanonicalReadMixin:
    @staticmethod
    def _read_v2_item_at(
        connection: sqlite3.Connection,
        stream: BinaryIO,
        *,
        sequence: int | None = None,
        offset: int,
        length: int,
        validated_commits: set[int] | None = None,
        validated_scopes: set[tuple[str, str]] | None = None,
    ) -> CanonicalItemRecord:
        """从 catalog 指向的 JSONL line 读取并校验唯一 canonical item。

        ``messages`` 只是 LangGraph 的投影定位表。任何 history/checkpoint
        恢复都必须先通过它定位 item，再从 JSONL/catalog 校验 identity、hash
        和不可变 envelope；envelope 不允许包含 message projection。
        """
        if sequence is not None:
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
        # 消息 locator 只有字节坐标；同次 catalog 查询解析 sequence，避免先定位再回查。
        predicate, parameters = (
            ("item.item_sequence=?", (sequence,)) if sequence is not None
            else ("item.jsonl_offset=? AND item.jsonl_length=? LIMIT 2", (offset, length))
        )
        rows = connection.execute(
            "SELECT item.item_id,item.content_hash,item.jsonl_offset,item.jsonl_length,"
            "item.projection_manifest_json,item.tool_call_id,item.payload_length,item.source_revision,"
            + CATALOG_READ_COLUMNS + ",item.item_sequence" + CATALOG_READ_JOINS + "WHERE " + predicate,
            parameters,
        ).fetchall()
        if sequence is None and len(rows) != 1:
            raise RuntimeError("message locator 必须恰好指向一个 canonical item")
        if not rows:
            raise RuntimeError(f"v2 item 缺少 item_catalog: sequence={sequence}")
        catalog = rows[0]
        if sequence is None:
            sequence = strict_non_negative_int(catalog[-1], field="item_catalog.item_sequence")
        item_id = strict_text(catalog[0], field="item_catalog.item_id")
        catalog_offset = strict_non_negative_int(
            catalog[2], field=f"item_catalog.jsonl_offset: {item_id}"
        )
        catalog_length = strict_non_negative_int(
            catalog[3], field=f"item_catalog.jsonl_length: {item_id}"
        )
        if catalog_length == 0:
            raise RuntimeError(
                f"item_catalog.jsonl_length 必须大于 0: {item_id}"
            )
        if catalog_offset != offset or catalog_length != length:
            raise RuntimeError(
                f"v2 item catalog 与 JSONL locator/hash 不一致: {item_id}"
            )
        validate_catalog_read_boundary(
            connection, catalog[8:-1], item_id=item_id, offset=offset, length=length,
            validated_commits=set() if validated_commits is None else validated_commits,
            validated_scopes=set() if validated_scopes is None else validated_scopes,
        )
        item = read_catalog_body(
            raw, sequence=sequence, item_id=item_id,
            catalog_hash=strict_text(catalog[1], field="item_catalog.content_hash"),
            payload_length=strict_non_negative_int(catalog[6], field="item_catalog.payload_length"),
            source_revision=strict_text(catalog[7], field="item_catalog.source_revision"),
        )
        relations = connection.execute(
            "SELECT canonical_relation_ordinal, relation, source_ref, target_ref "
            "FROM item_relations WHERE canonical_item_id = ? ORDER BY canonical_relation_ordinal",
            (item.item_id,),
        ).fetchall()
        validate_catalog_projection(
            item, connection=connection,
            projection_manifest_json=strict_text(catalog[4], field="item_catalog.projection_manifest_json"),
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
            item = self._read_v2_item_at(
                connection, stream, sequence=sequence, offset=offset, length=length,
            )
        if item.item_id != item_id:
            raise RuntimeError(f"canonical item catalog 与 JSONL identity 冲突: {item_id}")
        return item
