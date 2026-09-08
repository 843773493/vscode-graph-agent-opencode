"""sealed assembly snapshot 的读取 owner。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    json_text,
    non_negative_int,
    optional_text,
    required_text,
)


class ContextAssemblyReaderMixin:
    """只读取已提交 assembly，不读取当前 middleware 或实时 ledger。"""

    def get_context_assembly(
        self,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> ContextAssemblySnapshot:
        """读取已 sealed 的不可变 assembly snapshot。"""
        return self._read_committed_assembly(
            thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )[0]

    def get_context_assembly_detail_ref(
        self,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> DetailRef | None:
        """只返回同一只读快照中已验证的审计详情引用，不重新分配 locator。"""
        return self._read_committed_assembly(
            thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )[1]

    def _read_committed_assembly(
        self,
        thread_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str,
    ) -> tuple[ContextAssemblySnapshot, DetailRef | None]:
        required_text(assembly_id, field="assembly_id")
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT snapshot_json, status, plan_id, plan_hash, request_hash, turn_id, execution_id, history_view_revision, source_overlay_epoch, detail_ref FROM context_assemblies WHERE assembly_id = ? AND session_id = ?",
                (assembly_id, thread_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"context assembly 不存在: {assembly_id}")
            status = required_text(row[1], field="status")
            if status not in {"sealed", "terminal"}:
                raise RuntimeError(
                    f"context assembly manifest status 非法或未封存: {assembly_id}"
                )
            value = json_text(
                required_text(row[0], field="snapshot_json"),
                field="snapshot_json",
            )
            if not isinstance(value, Mapping):
                raise TypeError(f"context assembly snapshot 不是 object: {assembly_id}")
            snapshot = ContextAssemblySnapshot.from_dict(value)
            if snapshot.assembly_id != assembly_id or snapshot.session_id != thread_id:
                raise RuntimeError(f"context assembly identity 不一致: {assembly_id}")
            persisted_identity = (
                required_text(row[2], field="plan_id"),
                required_text(row[3], field="plan_hash"),
                required_text(row[4], field="request_hash"),
                required_text(row[5], field="turn_id"),
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
            self._validate_context_assembly_manifest(
                connection,
                snapshot,
                header_detail_ref=optional_text(row[9], field="detail_ref"),
                checkpoint_ns=checkpoint_ns,
            )
            try:
                snapshot.validate_hashes()
            except ItemSchemaError as error:
                raise RuntimeError(
                    f"context assembly hash 校验失败: {assembly_id}: {error}"
                ) from error
            header_key = optional_text(row[9], field="detail_ref")
            header_ref = detail_ref_from_key(header_key) if header_key is not None else None
            if header_ref is not None:
                header_ref.require_owner(thread_id, assembly_id)
            return snapshot, header_ref

    def list_context_assemblies(
        self,
        thread_id: str,
        *,
        turn_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> tuple[ContextAssemblySnapshot, ...]:
        """按 immutable creation order 返回已提交 assembly。"""
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            query = (
                "SELECT assembly_id FROM context_assemblies "
                "WHERE session_id = ? AND status IN ('sealed','terminal')"
            )
            params: list[object] = [thread_id]
            if turn_id is not None:
                query += " AND turn_id = ?"
                params.append(turn_id)
            query += " ORDER BY created_at, assembly_id"
            rows = connection.execute(query, tuple(params)).fetchall()
        # 列表读取也必须走与单个恢复相同的 header/manifest/hash 校验；不能
        # 因为批量接口直接解析 snapshot_json 而绕过 SQLite manifest。
        return tuple(
            self.get_context_assembly(
                thread_id,
                assembly_id=required_text(row[0], field="assembly_id"),
                checkpoint_ns=checkpoint_ns,
            )
            for row in rows
        )


__all__ = ["ContextAssemblyReaderMixin"]
