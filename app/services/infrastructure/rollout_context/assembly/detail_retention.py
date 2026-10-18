"""详情 retention 的 SQLite 引用保护与可恢复 tombstone 提交边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime

from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.activation.schema import (
    require_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    read_local_scopes,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


def _activation_referenced(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    detail_ref: str,
) -> bool:
    """activation catalog 引用同一受保护 detail 时也必须保护正文。

    activation snapshot 的 lineage manifest 与逐资源正文以 activation
    snapshot id（而不是 assembly id）作为 detail 的 assembly 段，因此无法被上面的
    context_assemblies 连接命中；retention 必须独立检查这几列，否则 sealed
    activation provenance 会被 tombstone 掉。当前 schema 缺失必须显式失败，
    只有已验证 schema 中不存在引用时才能回收。
    """
    require_resource_activation_schema(connection)
    if connection.execute(
        "SELECT 1 FROM resource_activation_snapshots WHERE session_id = ? "
        "AND thread_id = ? AND lineage_detail_ref = ? LIMIT 1",
        (session_id, thread_id, detail_ref),
    ).fetchone() is not None:
        return True
    return connection.execute(
        "SELECT 1 FROM resource_activation_bindings WHERE session_id = ? AND thread_id = ? "
        "AND (snapshot_ref = ? OR detail_ref = ?) LIMIT 1",
        (session_id, thread_id, detail_ref, detail_ref),
    ).fetchone() is not None


def _referenced(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    checkpoint_ns: str,
    detail_ref: str,
    assembly_id: str | None = None,
) -> bool:
    # assembly 整体诊断 detail 与每个 included source detail 都受保护。
    # 即使两个 selection 索引尚待完整性审计，只要任一仍引用正文就不能删。
    if connection.execute(
        "SELECT 1 FROM context_plan_details d JOIN context_assemblies a "
        "ON a.assembly_id = d.assembly_id AND a.session_id = d.session_id "
        "AND a.thread_id = d.thread_id "
        "WHERE d.session_id = ? AND d.thread_id = ? AND d.checkpoint_ns = ? AND d.detail_ref = ? "
        "AND (? IS NULL OR a.assembly_id = ?) AND a.status IN ('sealed','terminal') "
        "AND (a.detail_ref = d.detail_ref OR EXISTS ("
        "SELECT 1 FROM context_assembly_selections s WHERE s.assembly_id = a.assembly_id "
        "AND s.included = 1 AND s.detail_ref = d.detail_ref) OR EXISTS ("
        "SELECT 1 FROM assembly_item_refs r WHERE r.assembly_id = a.assembly_id "
        "AND r.detail_ref = d.detail_ref)) LIMIT 1",
        (session_id, thread_id, checkpoint_ns, detail_ref, assembly_id, assembly_id),
    ).fetchone() is not None:
        return True
    # sealed activation provenance/lineage 不是 context assembly 的附属 detail，
    # 必须由 activation catalog 自己的引用列保护。
    return _activation_referenced(connection, session_id, thread_id, detail_ref)


class DetailRetentionMixin:
    """GC 只允许处理无 sealed 引用的 detail，先持久标记再删除文件。"""

    def list_expired_context_plan_details(
        self,
        session_id: str,
        thread_id: str,
        *,
        expired_before: datetime,
        checkpoint_ns: str = "",
    ) -> tuple[tuple[DetailRef, str], ...]:
        session_id = strict_text(session_id, field="list_expired_details.session_id")
        thread_id = strict_text(thread_id, field="list_expired_details.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        if not isinstance(expired_before, datetime):
            raise TypeError("expired_before 必须是 datetime")
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                self._storage._require_v2_runtime(connection)
                if current_scope(connection) != (session_id, thread_id):
                    raise ValueError("source-mismatch: detail GC owner 与 rollout catalog 不一致")
                scopes = read_local_scopes(connection)
                rows = connection.execute(
                    "SELECT detail_ref, session_id, thread_id, assembly_id "
                    "FROM context_plan_details WHERE checkpoint_ns = ? "
                    "AND status IN ('available','unavailable') "
                    "AND expires_at IS NOT NULL AND expires_at < ? ORDER BY detail_ref",
                    (checkpoint_ns, expired_before.isoformat()),
                ).fetchall()
        except RolloutNotMaterializedError:
            return ()
        result: list[tuple[DetailRef, str]] = []
        for key, owner_session_id, owner_thread_id, assembly in rows:
            ref = detail_ref_from_key(key)
            assembly = strict_text(assembly, field="assembly_id")
            ref.require_owner(owner_session_id, owner_thread_id, assembly)
            if (owner_session_id, owner_thread_id) not in scopes:
                raise ValueError("source-mismatch: detail manifest 不属于当前 rollout 的冻结本地 scope")
            result.append((ref, assembly))
        return tuple(result)

    def context_assembly_references_detail(
        self,
        session_id: str,
        thread_id: str,
        *,
        assembly_id: str,
        detail_ref: DetailRef,
        checkpoint_ns: str = "",
    ) -> bool:
        session_id = strict_text(session_id, field="context_assembly_references.session_id")
        thread_id = strict_text(thread_id, field="context_assembly_references.thread_id")
        assembly_id = strict_text(assembly_id, field="assembly_id")
        key = detail_ref_key(detail_ref)
        if detail_ref.assembly_id != assembly_id:
            raise ValueError("source-mismatch: detail_ref 不属于请求的 assembly")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                self._storage._require_v2_runtime(connection)
                require_historical_scope(
                    connection, detail_ref.session_id, detail_ref.thread_id,
                    current_owner=(session_id, thread_id),
                )
                return _referenced(
                    connection, detail_ref.session_id, detail_ref.thread_id,
                    checkpoint_ns, key, assembly_id,
                )
        except RolloutNotMaterializedError:
            return False

    def mark_context_plan_details_unavailable(
        self,
        session_id: str,
        thread_id: str,
        *,
        detail_refs: Iterable[DetailRef],
        checkpoint_ns: str = "",
    ) -> None:
        session_id = strict_text(session_id, field="mark_details_unavailable.session_id")
        thread_id = strict_text(thread_id, field="mark_details_unavailable.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        keys: list[str] = []
        owners: dict[str, tuple[str, str]] = {}
        ref_by_key: dict[str, DetailRef] = {}
        for ref in detail_refs:
            if not isinstance(ref, DetailRef):
                raise TypeError("detail_refs 只能包含 DetailRef")
            key = detail_ref_key(ref)
            owners[key] = (ref.session_id, ref.thread_id)
            ref_by_key[key] = ref
            keys.append(key)
        refs = tuple(dict.fromkeys(keys))
        if not refs:
            return
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection:
                self._storage._require_v2_runtime(connection)
                if current_scope(connection) != (session_id, thread_id):
                    raise ValueError("source-mismatch: detail GC owner 与 rollout catalog 不一致")
                for ref_session_id, ref_thread_id in owners.values():
                    require_historical_scope(
                        connection, ref_session_id, ref_thread_id,
                        current_owner=(session_id, thread_id),
                    )
                for key, ref in ref_by_key.items():
                    row = connection.execute(
                        "SELECT session_id, thread_id, assembly_id, checkpoint_ns "
                        "FROM context_plan_details WHERE detail_ref = ?",
                        (key,),
                    ).fetchone()
                    if row != (ref.session_id, ref.thread_id, ref.assembly_id, checkpoint_ns):
                        raise RuntimeError("detail tombstone 目标缺失或 identity 不一致")
                # 候选扫描和实际 tombstone 之间可能发生 seal；在同一个写锁内
                # 重新检查，不能依赖 GC 调用方之前拿到的过期候选快照。
                protected = tuple(
                    ref for ref in refs
                    if _referenced(
                        connection, *owners[ref], checkpoint_ns, ref
                    )
                )
                if protected:
                    raise RuntimeError(
                        "detail-retention-protected: committed context source 仍引用 detail: "
                        + ",".join(protected)
                    )
                placeholders = ",".join("?" for _ in refs)
                result = connection.execute(
                    "UPDATE context_plan_details SET status = 'unavailable', availability = 'unavailable' "
                    f"WHERE checkpoint_ns = ? AND detail_ref IN ({placeholders})",
                    (checkpoint_ns, *refs),
                )
                if result.rowcount != len(refs):
                    raise RuntimeError("detail tombstone 目标缺失或 identity 不一致")
                connection.commit()
