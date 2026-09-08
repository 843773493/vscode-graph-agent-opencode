"""fork 的 typed detail identity 映射；SQLite key 与叶子 ID 不可混用。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.fork.validation import required_text


def collect_detail_mappings(
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
    assembly_map: Mapping[str, str],
    allocate: Callable[[str], str],
) -> dict[str, str]:
    """按完整 source owner 坐标分配 target leaf，不能用 JCS key 当文件名。"""
    result = {}
    target_keys: set[str] = set()
    for key, assembly_id, detail_id in connection.execute(
        "SELECT detail_ref, assembly_id, detail_id FROM context_plan_details"
    ).fetchall():
        ref = validate_detail_row_identity(
            key, assembly_id, detail_id, session_id=source_session_id
        )
        target_assembly = assembly_map.get(ref.assembly_id)
        if target_assembly is None:
            raise RuntimeError("source-mismatch: fork detail 缺少 assembly mapping")
        target = DetailRef(target_session_id, target_assembly, allocate(key))
        target_key = detail_ref_key(target)
        if target_key in target_keys:
            raise RuntimeError("source-mismatch: fork detail target identity 冲突")
        target_keys.add(target_key)
        result[key] = target_key
    return result


def mapped_detail_ref(
    ref: DetailRef,
    *,
    source_session_id: str,
    target_session_id: str,
    detail_map: Mapping[str, str],
) -> DetailRef:
    if not isinstance(ref, DetailRef):
        raise TypeError("fork detail reference 必须是 DetailRef")
    ref.require_owner(source_session_id)
    target_key = detail_map.get(detail_ref_key(ref))
    if target_key is None:
        raise RuntimeError("source-mismatch: fork detail 未登记 target-local mapping")
    target = detail_ref_from_key(target_key)
    target.require_owner(target_session_id)
    return target


def validate_detail_row_identity(
    key: object, assembly_id: object, detail_id: object, *, session_id: str
) -> DetailRef:
    ref = detail_ref_from_key(key)
    ref.require_owner(
        session_id, required_text(assembly_id, field="detail.assembly_id")
    )
    if ref.detail_id != required_text(detail_id, field="detail.detail_id"):
        raise RuntimeError("source-mismatch: fork detail identity 列不一致")
    return ref
