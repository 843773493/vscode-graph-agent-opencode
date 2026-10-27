"""sealed assembly snapshot 的读取 owner。"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.runtime_wakeup import RuntimeInputConsumption
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    optional_detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    _ValidatedPredecessors,
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    non_negative_int,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class ContextAssemblyReaderMixin:
    """只读取已提交 assembly，不读取当前 middleware 或实时 ledger。"""

    _storage: RolloutStorage

    def runtime_input_consumption(
        self, session_id: str, thread_id: str, *, item_id: str,
        source_id: str, source_revision: str | None = None,
    ) -> RuntimeInputConsumption | None:
        """按精确 item 或来源版本查派发证据，候选索引仍须通过原 assembly 校验。"""
        if source_revision is None:
            predicate = "selection.ref_type='canonical_item' AND selection.ref_id=? AND selection.ref_thread_id=?"
            values = (item_id, thread_id)
        else:
            predicate = "json_extract(contribution.binding_manifest_json,'$.source_binding.source_ref')=? AND contribution.source_revision=?"
            values = (source_id, source_revision)
        with self._storage.open_read_snapshot(session_id, thread_id, "") as snapshot:
            connection = snapshot.connection
            row = connection.execute(
                "SELECT assembly.assembly_id,call.model_call_id,call.execution_id FROM context_assemblies assembly "
                "JOIN model_calls call ON call.assembly_id=assembly.assembly_id "
                "AND call.session_id=assembly.session_id AND call.thread_id=assembly.thread_id "
                "AND call.execution_id=assembly.execution_id "
                "WHERE assembly.session_id=? AND assembly.thread_id=? AND call.dispatch_state IN ('dispatched','completed') "
                "AND assembly.status IN ('sealed','terminal') "
                "AND json_extract(assembly.snapshot_json,'$.selection_policy')<>'summary_candidate' AND EXISTS ("
                "SELECT 1 FROM context_assembly_selections selection LEFT JOIN context_assembly_contributions contribution "
                "ON contribution.assembly_id=selection.assembly_id AND contribution.contribution_id=selection.contribution_id "
                "WHERE selection.assembly_id=assembly.assembly_id AND selection.included=1 AND " + predicate + ") "
                "ORDER BY assembly.rowid,call.attempt LIMIT 1", (session_id, thread_id, *values),
            ).fetchone()
            if row is None:
                return None
            assembly, _ = self._read_context_assembly_connection(
                connection, session_id, thread_id, assembly_id=row[0], checkpoint_ns="",
            )
            if source_revision is None:
                included = any(entry.included and entry.ref.ref_type == "canonical_item"
                               and entry.ref.ref_id == item_id and entry.ref.thread_id == thread_id for entry in assembly.selection)
            else:
                included = assembly.seal_binding is not None and any(
                    source.source_id == source_id and source.revision == source_revision
                    for source in assembly.seal_binding.source_revisions
                )
            if not included:
                raise RuntimeError("输入消费索引与封存 assembly 不一致")
            return RuntimeInputConsumption(row[2], row[1], row[0], assembly.request_hash)

    def get_context_assembly(
        self,
        session_id: str,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> ContextAssemblySnapshot:
        """读取已 sealed 的不可变 assembly snapshot。"""
        return self._read_committed_assembly(
            session_id, thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )[0]

    def get_context_assembly_detail_ref(
        self,
        session_id: str,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> DetailRef | None:
        """只返回同一只读快照中已验证的审计详情引用，不重新分配 locator。"""
        return self._read_committed_assembly(
            session_id, thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )[1]

    def _read_committed_assembly(
        self,
        session_id: str,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str,
    ) -> tuple[ContextAssemblySnapshot, DetailRef | None]:
        required_text(assembly_id, field="assembly_id")
        with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
            return self._read_context_assembly_connection(
                snapshot.connection, session_id, thread_id,
                assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
            )

    def latest_context_assembly(
        self, snapshot: RolloutReadSnapshot,
        *, validated_predecessors: _ValidatedPredecessors | None = None,
    ) -> ContextAssemblySnapshot | None:
        """在 caller 的同一 SQLite snapshot 中读取并验证唯一 applied predecessor。"""
        return self._latest_context_assembly_connection(
            snapshot.connection, snapshot.session_id, snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            validated_predecessors=validated_predecessors,
        )

    def _latest_context_assembly_connection(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        excluding_assembly_id: str | None = None,
        validated_predecessors: _ValidatedPredecessors | None = None,
    ) -> ContextAssemblySnapshot | None:
        """同一读或 seal 事务共用精确可见的历史 predecessor。"""
        assembly_id = self._latest_context_assembly_id_connection(
            connection, session_id, thread_id, checkpoint_ns=checkpoint_ns,
            excluding_assembly_id=excluding_assembly_id,
        )
        if assembly_id is None:
            return None
        return self._read_context_assembly_connection(
            connection, session_id, thread_id,
            assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
            validated_predecessors=validated_predecessors,
        )[0]

    def _latest_context_assembly_id_connection(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        excluding_assembly_id: str | None = None,
    ) -> str | None:
        """只判定当前 snapshot 的前驱身份；调用方负责完整校验。"""
        # 导入请求只有真实输出在当前 view 可见时才能继承，正文 backing 不扩大前缀。
        row = connection.execute(
            "SELECT assembly.assembly_id FROM context_assemblies assembly "
            "WHERE assembly.status IN ('sealed','terminal') "
            "AND json_extract(assembly.snapshot_json,'$.selection_policy') <> 'summary_candidate' "
            "AND (? IS NULL OR assembly.assembly_id<>?) "
            "AND ((assembly.session_id=? AND assembly.thread_id=?) OR EXISTS ("
            "SELECT 1 FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
            "JOIN branches branch ON branch.head_view_id=member.view_id "
            "JOIN checkpoint_namespace_state namespace ON namespace.active_branch_id=branch.branch_id "
            "WHERE namespace.checkpoint_ns=? AND member.visible=1 "
            "AND COALESCE(json_extract(item.producer_ref_json,'$.model_call_id'),"
            "json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id'))=assembly.model_call_id)) "
            "ORDER BY assembly.rowid DESC LIMIT 1",
            (excluding_assembly_id, excluding_assembly_id, session_id, thread_id, checkpoint_ns),
        ).fetchone()
        return None if row is None else required_text(row[0], field="assembly_id")

    def _read_context_assembly_connection(
        self, connection: sqlite3.Connection, session_id: str, thread_id: str,
        *, assembly_id: str, checkpoint_ns: str,
        registration: ContextPlanRegistration | None = None,
        validated_predecessors: _ValidatedPredecessors | None = None,
    ) -> tuple[ContextAssemblySnapshot, DetailRef | None]:
        snapshot, header_ref, registration = self._read_context_assembly_header(
            connection, session_id, thread_id, assembly_id=assembly_id,
            registration=registration,
            validated_predecessors=validated_predecessors,
        )
        self._validate_context_assembly_manifest(
            connection,
            snapshot,
            registration=registration,
            header_detail_ref=optional_detail_ref_key(header_ref),
            checkpoint_ns=checkpoint_ns,
            validated_predecessors=validated_predecessors,
        )
        return snapshot, header_ref

    def _read_context_assembly_header(
        self, connection: sqlite3.Connection, session_id: str, thread_id: str,
        *, assembly_id: str,
        registration: ContextPlanRegistration | None = None,
        validated_predecessors: _ValidatedPredecessors | None = None,
    ) -> tuple[ContextAssemblySnapshot, DetailRef | None, ContextPlanRegistration]:
        """精确校验封存前驱的身份和 registry hash，不递归展开历史前驱链。

        session_id/thread_id 是当前目标物理存储 owner，historical_owner 是
        snapshot 保留的原执行 scope；binding 仅核准历史 scope，不定位源库。
        """
        self._storage._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT snapshot_json, status, plan_id, plan_hash, request_hash, turn_id, execution_id, history_view_revision, source_overlay_epoch, detail_ref, session_id, thread_id FROM context_assemblies WHERE assembly_id = ?",
            (assembly_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"context assembly 不存在: {assembly_id}")
        historical_owner = (
            required_text(row[10], field="context_assemblies.session_id"),
            required_text(row[11], field="context_assemblies.thread_id"),
        )
        require_historical_scope(
            connection, *historical_owner, current_owner=(session_id, thread_id),
        )
        status = required_text(row[1], field="status")
        if status not in {"sealed", "terminal"}:
            raise RuntimeError(
                f"context assembly manifest status 非法或未封存: {assembly_id}"
            )
        # 只允许同一 SQLite 读快照的 caller 传入刚完成冷校验的 registry；
        # header/detail/selection 的完整 reader 校验仍在本次连接内完成。
        if registration is None:
            registration = read_registration(
                connection, *historical_owner, required_text(row[2], field="plan_id"),
                validated_predecessors=validated_predecessors,
            )
        if (registration.session_id, registration.thread_id, registration.draft.plan_id) != (
            *historical_owner, row[2],
        ):
            raise RuntimeError(f"source-mismatch: context assembly registry owner 不一致: {assembly_id}")
        snapshot = registration.sealed_snapshot
        if snapshot is None:
            raise RuntimeError(f"source-mismatch: context assembly 的 plan 尚未 sealed: {assembly_id}")
        if (snapshot.assembly_id, snapshot.session_id, snapshot.thread_id) != (
            assembly_id,
            *historical_owner,
        ):
            raise RuntimeError(f"context assembly identity 不一致: {assembly_id}")
        persisted_identity = (
            required_text(row[2], field="plan_id"),
            required_text(row[3], field="plan_hash"),
            required_text(row[4], field="request_hash"),
            optional_text(row[5], field="turn_id"),
            required_text(row[6], field="execution_id"),
            non_negative_int(row[7], field="history_view_revision"),
            non_negative_int(row[8], field="source_overlay_epoch"),
        )
        if persisted_identity != (
            snapshot.plan_id,
            snapshot.plan_hash,
            snapshot.request_hash,
            snapshot.turn_id,
            snapshot.execution_id,
            snapshot.history_view_revision,
            snapshot.source_overlay_epoch,
        ):
            raise RuntimeError(
                f"context assembly header 与 snapshot 不一致: {assembly_id}"
            )
        header_key = optional_text(row[9], field="detail_ref")
        header_ref = detail_ref_from_key(header_key) if header_key is not None else None
        if header_ref is not None:
            header_ref.require_owner(*historical_owner, assembly_id)
        return snapshot, header_ref, registration


    def list_context_assemblies(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> tuple[ContextAssemblySnapshot, ...]:
        """按 immutable creation order 返回已提交 assembly。"""
        with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
            query = (
                "SELECT assembly_id FROM context_assemblies "
                "WHERE status IN ('sealed','terminal')"
            )
            params: list[object] = []
            if turn_id is not None:
                query += " AND turn_id = ?"
                params.append(turn_id)
            query += " ORDER BY rowid"
            rows = snapshot.connection.execute(query, tuple(params)).fetchall()
            return tuple(
                self._read_context_assembly_connection(
                    snapshot.connection, session_id, thread_id,
                    assembly_id=required_text(row[0], field="assembly_id"),
                    checkpoint_ns=checkpoint_ns,
                )[0]
                for row in rows
            )


__all__ = ["ContextAssemblyReaderMixin"]
