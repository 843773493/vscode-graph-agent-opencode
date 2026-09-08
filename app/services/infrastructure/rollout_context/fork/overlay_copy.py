"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from app.services.infrastructure.rollout_context.assembly.overlays import (
    _validate_source_overlay_row,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ForkOverlayCopyMixin:
    """非 full-copy 模式的 source overlay registry 本地化。"""

    def _copy_source_overlays_for_fork(
        self,
        target_connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        """将非 full fork 的 source overlay 重新编号为 target-local。

        overlay 正文仍由 Saver 的实时 request-only ledger 持有；这里只复制
        source revision/epoch/lineage 和 target-local ref。正文缺失时，后续
        projector 会返回 capability loss，而不会偷偷读取 source session。
        """
        source_root = self.root(source_session_id, checkpoint_ns)
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(
                source_session_id,
                checkpoint_ns,
                read_only=True,
            ) as source_connection:
                overlay_columns = (
                    "overlay_id",
                    "source_kind",
                    "source_revision",
                    "source_overlay_epoch",
                    "base_ref",
                    "delta_ref",
                    "base_source_revision",
                    "base_content_length",
                    "base_content_hash",
                    "base_redacted_stable_digest",
                    "delta_source_revision",
                    "delta_content_length",
                    "delta_content_hash",
                    "delta_redacted_stable_digest",
                    "delta_from_revision",
                    "delta_to_revision",
                    "delta_diff_hash",
                    "supersedes_overlay_id",
                    "materializes_overlay_id",
                    "status",
                    "idempotency_key",
                    "created_at",
                )
                raw_rows = source_connection.execute(
                    "SELECT overlay_id, source_kind, source_revision, source_overlay_epoch, base_ref, delta_ref, base_source_revision, base_content_length, base_content_hash, base_redacted_stable_digest, delta_source_revision, delta_content_length, delta_content_hash, delta_redacted_stable_digest, delta_from_revision, delta_to_revision, delta_diff_hash, supersedes_overlay_id, materializes_overlay_id, status, idempotency_key, created_at FROM source_overlays WHERE session_id = ? AND checkpoint_ns = ? ORDER BY source_overlay_epoch, created_at, overlay_id",
                    (source_session_id, checkpoint_ns),
                ).fetchall()
                rows = tuple(
                    tuple(row[:21])
                    for row in raw_rows
                    if _validate_source_overlay_row(
                        dict(zip(overlay_columns, row, strict=True)),
                        session_id=source_session_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                    is None
                )
        finally:
            source_lock.release()
        if not rows:
            return
        source_epochs = sorted(
            {
                non_negative_int(row[3], field="source_overlay.source_overlay_epoch")
                for row in rows
            }
        )
        target_meta = target_connection.execute(
            "SELECT source_overlay_epoch FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if target_meta is None:
            raise RuntimeError(
                "target database_meta singleton 缺失，不能复制 source overlay"
            )
        target_epoch_base = non_negative_int(
            target_meta[0],
            field="target.database_meta.source_overlay_epoch",
        )
        epoch_map = {
            source_epoch: target_epoch_base + index
            for index, source_epoch in enumerate(source_epochs)
        }
        overlay_map = {
            row[0]: (
                f"fork-overlay:{target_session_id}:"
                f"{hashlib.sha256((fork_id + ':' + row[0]).encode('utf-8')).hexdigest()[:32]}"
            )
            for row in rows
        }

        def remap_ref(value: object) -> str | None:
            if not isinstance(value, str) or not value:
                return None
            return (
                f"fork-source-ref:{target_session_id}:"
                f"{hashlib.sha256((fork_id + ':' + value).encode('utf-8')).hexdigest()[:32]}"
            )

        for (
            source_overlay_id,
            source_kind,
            source_revision,
            source_epoch,
            base_ref,
            delta_ref,
            base_source_revision,
            base_content_length,
            base_content_hash,
            base_redacted_stable_digest,
            delta_source_revision,
            delta_content_length,
            delta_content_hash,
            delta_redacted_stable_digest,
            delta_from_revision,
            delta_to_revision,
            delta_diff_hash,
            supersedes_id,
            materializes_id,
            status,
            source_idempotency_key,
        ) in rows:
            target_overlay_id = overlay_map[source_overlay_id]
            result = target_connection.execute(
                "INSERT INTO source_overlays(overlay_id, session_id, checkpoint_ns, source_kind, source_revision, source_overlay_epoch, base_ref, delta_ref, base_source_revision, base_content_length, base_content_hash, base_redacted_stable_digest, delta_source_revision, delta_content_length, delta_content_hash, delta_redacted_stable_digest, delta_from_revision, delta_to_revision, delta_diff_hash, supersedes_overlay_id, materializes_overlay_id, status, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target_overlay_id,
                    target_session_id,
                    checkpoint_ns,
                    source_kind,
                    source_revision,
                    epoch_map[source_epoch],
                    remap_ref(base_ref),
                    remap_ref(delta_ref),
                    base_source_revision,
                    base_content_length,
                    base_content_hash,
                    base_redacted_stable_digest,
                    delta_source_revision,
                    delta_content_length,
                    delta_content_hash,
                    delta_redacted_stable_digest,
                    delta_from_revision,
                    delta_to_revision,
                    delta_diff_hash,
                    overlay_map.get(supersedes_id) if supersedes_id else None,
                    overlay_map.get(materializes_id) if materializes_id else None,
                    status,
                    f"fork-overlay:{fork_id}:{source_idempotency_key or source_overlay_id}",
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork source overlay 未写入 target: {target_overlay_id}"
                )
            lineage = {
                "identity_mode": "remapped_target_local",
                "source": {
                    "session_id": source_session_id,
                    "overlay_id": source_overlay_id,
                    "source_overlay_epoch": source_epoch,
                    "base_ref": base_ref,
                    "delta_ref": delta_ref,
                },
                "target": {
                    "session_id": target_session_id,
                    "overlay_id": target_overlay_id,
                    "source_overlay_epoch": epoch_map[source_epoch],
                    "base_ref": remap_ref(base_ref),
                    "delta_ref": remap_ref(delta_ref),
                },
                "fork_id": fork_id,
            }
            result = target_connection.execute(
                "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'source_overlay', ?, ?, NULL, NULL, ?, ?)",
                (
                    uuid4().hex,
                    fork_id,
                    source_session_id,
                    target_session_id,
                    source_overlay_id,
                    target_overlay_id,
                    _json(lineage),
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork source overlay lineage 未写入: {target_overlay_id}"
                )
        result = target_connection.execute(
            "UPDATE database_meta SET source_overlay_epoch = MAX(source_overlay_epoch, ?), updated_at = ? WHERE singleton_id = 1",
            (max(epoch_map.values()), timestamp),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork source_overlay_epoch 未写入 database_meta")
