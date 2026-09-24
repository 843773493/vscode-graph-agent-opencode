"""详情 retention 的 SQLite 引用保护与可恢复 tombstone 提交边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _activation_referenced(
    connection: sqlite3.Connection,
    session_id: str,
    detail_ref: str,
) -> bool:
    """activation catalog 引用同一受保护 detail 时也必须保护正文。

    activation snapshot 的 lineage manifest 与逐资源正文以 activation
    snapshot id（而不是 assembly id）作为 detail 的 assembly 段，因此无法被上面的
    context_assemblies 连接命中；retention 必须独立检查这几列，否则 sealed
    activation provenance 会被 tombstone 掉。schema 未 bootstrap 时该库没有
    activation 事实，直接视为无引用。
    """
    if not _table_exists(connection, "resource_activation_snapshots"):
        return False
    if connection.execute(
        "SELECT 1 FROM resource_activation_snapshots WHERE session_id = ? "
        "AND lineage_detail_ref = ? LIMIT 1",
        (session_id, detail_ref),
    ).fetchone() is not None:
        return True
    if not _table_exists(connection, "resource_activation_bindings"):
        return False
    return connection.execute(
        "SELECT 1 FROM resource_activation_bindings WHERE session_id = ? "
        "AND (snapshot_ref = ? OR detail_ref = ?) LIMIT 1",
        (session_id, detail_ref, detail_ref),
    ).fetchone() is not None


def _referenced(
    connection: sqlite3.Connection,
    session_id: str,
    checkpoint_ns: str,
    detail_ref: str,
    assembly_id: str | None = None,
) -> bool:
    # assembly 整体诊断 detail 与每个 included source detail 都受保护。
    # 即使两个 selection 索引尚待完整性审计，只要任一仍引用正文就不能删。
    if connection.execute(
        "SELECT 1 FROM context_plan_details d JOIN context_assemblies a "
        "ON a.assembly_id = d.assembly_id AND a.session_id = d.session_id "
        "WHERE d.session_id = ? AND d.checkpoint_ns = ? AND d.detail_ref = ? "
        "AND (? IS NULL OR a.assembly_id = ?) AND a.status IN ('sealed','terminal') "
        "AND (a.detail_ref = d.detail_ref OR EXISTS ("
        "SELECT 1 FROM context_assembly_selections s WHERE s.assembly_id = a.assembly_id "
        "AND s.included = 1 AND s.detail_ref = d.detail_ref) OR EXISTS ("
        "SELECT 1 FROM assembly_item_refs r WHERE r.assembly_id = a.assembly_id "
        "AND r.detail_ref = d.detail_ref)) LIMIT 1",
        (session_id, checkpoint_ns, detail_ref, assembly_id, assembly_id),
    ).fetchone() is not None:
        return True
    # sealed activation provenance/lineage 不是 context assembly 的附属 detail，
    # 必须由 activation catalog 自己的引用列保护。
    return _activation_referenced(connection, session_id, detail_ref)


class DetailRetentionMixin:
    """GC 只允许处理无 sealed 引用的 detail，先持久标记再删除文件。"""

    def list_expired_context_plan_details(
        self,
        thread_id: str,
        *,
        expired_before: datetime,
        checkpoint_ns: str = "",
    ) -> tuple[tuple[DetailRef, str], ...]:
        thread_id = strict_text(thread_id, field="list_expired_details.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        if not isinstance(expired_before, datetime):
            raise TypeError("expired_before 必须是 datetime")
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT detail_ref, assembly_id FROM context_plan_details "
                "WHERE session_id = ? AND checkpoint_ns = ? "
                "AND status IN ('available','unavailable') "
                "AND expires_at IS NOT NULL AND expires_at < ? ORDER BY detail_ref",
                (thread_id, checkpoint_ns, expired_before.isoformat()),
            ).fetchall()
        result: list[tuple[DetailRef, str]] = []
        for key, assembly in rows:
            ref = detail_ref_from_key(key)
            assembly = strict_text(assembly, field="assembly_id")
            ref.require_owner(thread_id, assembly)
            result.append((ref, assembly))
        return tuple(result)

    def context_assembly_references_detail(
        self,
        thread_id: str,
        *,
        assembly_id: str,
        detail_ref: DetailRef,
        checkpoint_ns: str = "",
    ) -> bool:
        thread_id = strict_text(thread_id, field="context_assembly_references.thread_id")
        assembly_id = strict_text(assembly_id, field="assembly_id")
        key = detail_ref_key(detail_ref)
        detail_ref.require_owner(thread_id, assembly_id)
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            return _referenced(connection, thread_id, checkpoint_ns, key, assembly_id)

    def mark_context_plan_details_unavailable(
        self,
        thread_id: str,
        *,
        detail_refs: Iterable[DetailRef],
        checkpoint_ns: str = "",
    ) -> None:
        thread_id = strict_text(thread_id, field="mark_details_unavailable.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        keys: list[str] = []
        for ref in detail_refs:
            key = detail_ref_key(ref)
            ref.require_owner(thread_id)
            keys.append(key)
        refs = tuple(dict.fromkeys(keys))
        if not refs:
            return
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                # 候选扫描和实际 tombstone 之间可能发生 seal；在同一个写锁内
                # 重新检查，不能依赖 GC 调用方之前拿到的过期候选快照。
                protected = tuple(
                    ref for ref in refs
                    if _referenced(connection, thread_id, checkpoint_ns, ref)
                )
                if protected:
                    raise RuntimeError(
                        "detail-retention-protected: sealed assembly 仍引用 detail: "
                        + ",".join(protected)
                    )
                placeholders = ",".join("?" for _ in refs)
                result = connection.execute(
                    "UPDATE context_plan_details SET status = 'unavailable', availability = 'unavailable' "
                    f"WHERE session_id = ? AND checkpoint_ns = ? AND detail_ref IN ({placeholders})",
                    (thread_id, checkpoint_ns, *refs),
                )
                if result.rowcount != len(refs):
                    raise RuntimeError("detail tombstone 目标缺失或 identity 不一致")
                connection.commit()
