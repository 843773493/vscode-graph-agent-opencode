"""消息 locator 到完整 canonical group 的受校验读取，不进行消息投影。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import BinaryIO

from app.domain.itemized.identity.item_projection import ItemProjectionManifest
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)

MessageLocator = tuple[int, str | None, str, int, int]


def count_uncompacted_messages(
    connection: sqlite3.Connection, item_ids: Sequence[str], *, checkpoint_ns: str,
) -> int | None:
    """只展开实际消息选集的扁平摘要 provenance，按原 canonical group 计数。"""
    selected = tuple(item_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("原消息计数要求非空且不重复的真实 canonical anchor")
    rows = connection.execute(
        "SELECT item_id,semantic_kind,projection_manifest_json FROM item_catalog "
        "WHERE item_id IN (SELECT value FROM json_each(?))", (canonical_json_text(selected),),
    ).fetchall()
    if len(rows) != len(selected):
        raise ValueError("detail-unavailable: 原消息计数缺少实际选集中的 canonical anchor")
    originals: set[str] = set()
    for item_id, kind, manifest_json in rows:
        if kind != "compaction_summary":
            originals.add(item_id)
            continue
        manifest = ItemProjectionManifest.from_dict(json.loads(manifest_json))
        origins = [relation.to_dict() for relation in manifest.provenance_relations
                   if relation.relation == "derived_from" and relation.target_kind == "canonical_item"]
        if not origins:
            raise ValueError("compaction-coverage-mismatch: 摘要缺少扁平 canonical provenance")
        # provenance 还包含派生输出；只有实际采用时登记的原文范围属于原消息计数。
        coverage = connection.execute(
            "SELECT history.item_id FROM json_each(?) origin JOIN compaction_history_ranges history "
            "ON history.item_id=json_extract(origin.value,'$.target_id') "
            "AND json_extract(history.ref_json,'$.session_id')=json_extract(origin.value,'$.target_session_id') "
            "AND json_extract(history.ref_json,'$.thread_id')=json_extract(origin.value,'$.target_thread_id') "
            "WHERE history.checkpoint_ns=?",
            (canonical_json_text(origins), checkpoint_ns),
        ).fetchall()
        if not coverage:
            raise ValueError("compaction-coverage-mismatch: 摘要缺少原采用事件的消息范围")
        originals.update(row[0] for row in coverage)
        if connection.execute(
            "SELECT 1 FROM json_each(?) origin JOIN item_catalog item "
            "ON item.item_id=json_extract(origin.value,'$.target_id') "
            "WHERE item.semantic_kind='compaction_summary' LIMIT 1", (canonical_json_text(origins),),
        ).fetchone() is not None:
            raise ValueError("compaction-coverage-mismatch: 摘要 provenance 必须扁平到原 canonical Item")
    count, present, nested = connection.execute(
        "SELECT COUNT(DISTINCT CASE WHEN NOT EXISTS (SELECT 1 FROM storage_commits admission "
        "WHERE admission.subject_id=item.execution_id AND admission.commit_kind='execution_admission' "
        "AND admission.status='committed' AND json_extract(admission.metadata_json,'$.request_input.kind')='derived_reference') "
        "AND (EXISTS (SELECT 1 FROM messages message WHERE message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length) "
        "OR item.message_group_id IS NOT NULL AND EXISTS (SELECT 1 FROM item_catalog anchor JOIN messages message "
        "ON message.jsonl_offset=anchor.jsonl_offset AND message.jsonl_length=anchor.jsonl_length "
        "WHERE anchor.message_group_id=item.message_group_id)) THEN COALESCE(item.message_group_id,item.item_id) END),"
        "COUNT(*),COALESCE(SUM(item.semantic_kind='compaction_summary'),0) FROM item_catalog item "
        "WHERE item.item_id IN (SELECT value FROM json_each(?))", (canonical_json_text(sorted(originals)),),
    ).fetchone()
    if nested:
        raise ValueError("compaction-coverage-mismatch: 摘要 provenance 必须扁平到原 canonical Item")
    if present != len(originals):
        # summary-only fork 合法保留摘要而不复制原消息组；不可得的计数不能阻断当前选集。
        return None
    return count


def find_message_locator(
    connection: sqlite3.Connection, message_id: str, *,
    item_id: str | None = None, sequences: Sequence[int] | None = None,
) -> MessageLocator | None:
    """logical ID 只用于校验；已知 item/view 时必须按其冻结坐标解析版本。"""
    predicates = ["message.message_id=?"]
    parameters: list[object] = [message_id]
    if item_id is not None:
        predicates.append("item.item_id=?")
        parameters.append(item_id)
    if sequences is not None:
        allowed = tuple(dict.fromkeys(sequences))
        predicates.append(f"message.message_sequence IN ({','.join('?' for _ in allowed)})")
        parameters.extend(allowed)
    # 先按冻结 view 筛选版本，再连接 catalog；不能为一个旧版本遍历全部历史版本。
    # 两条候选已足以证明歧义，禁止先物化完整版本集合再在 Python 过滤。
    rows = connection.execute(
        "SELECT message.message_sequence,message.turn_id,message.execution_id,"
        "message.jsonl_offset,message.jsonl_length,item.item_id,item.turn_id,item.execution_id "
        "FROM messages message LEFT JOIN item_catalog item "
        "ON item.jsonl_offset=message.jsonl_offset AND item.jsonl_length=message.jsonl_length "
        "WHERE " + " AND ".join(predicates) + " LIMIT 2",
        parameters,
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"message locator 版本歧义，必须提供精确 canonical item/view: {message_id}")
    if rows and rows[0][5] is None:
        raise RuntimeError(f"message projection 缺少 canonical item: {message_id}")
    if not rows:
        if item_id is not None:
            raise RuntimeError(f"message locator 缺少指定 canonical item: {message_id}/{item_id}")
        return None
    # locator 已连接唯一 anchor，同次核对归属，消费者无需再按字节坐标回查 catalog。
    if rows[0][1:3] != rows[0][6:8]:
        raise RuntimeError(f"message projection 与 canonical item attribution 不一致: {message_id}")
    return tuple(rows[0][:5])


def read_message_group(
    connection: sqlite3.Connection, stream: BinaryIO, *,
    offset: int, length: int,
    read_item: Callable[..., CanonicalItemRecord],
) -> tuple[CanonicalItemRecord, ...]:
    """message_sequence 与 item_sequence 独立；先解析 anchor 的真实坐标。"""
    anchor = read_item(
        connection, stream, offset=offset, length=length,
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
