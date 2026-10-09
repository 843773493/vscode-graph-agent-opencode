"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

from uuid import uuid4

from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.fork.assembly_copy import (
    prepare_selected_detail_mappings,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    one_of_text,
    optional_identity,
    optional_text,
    required_sha256,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class ForkMaterializationJournalMixin:
    """fork journal 的 prepare、abort、completion 与 committed proof。"""

    def begin_fork_materialization(
        self,
        *,
        target_session_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str,
        source_thread_id: str,
        target_thread_id: str,
        source_snapshot_sha256: str,
        fork_id: str | None = None,
        materialization_id: str | None = None,
    ) -> tuple[str, str]:
        """为目标 rollout 建立一次可恢复的 fork 物化 journal。"""
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        if target_session_id == source_session_id:
            raise ValueError("fork target_session_id 不能与 source_session_id 相同")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_mode = one_of_text(
            fork_mode,
            {"context_fork", "history_prefix_fork", "full_rollout_copy"},
            field="fork_mode",
        )
        relationship = one_of_text(
            relationship, {"detached", "pinned"}, field="relationship"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        source_snapshot_sha256 = required_sha256(
            source_snapshot_sha256, field="source_snapshot_sha256"
        )
        self._storage._resolve_thread_node(source_session_id, source_thread_id)
        if self._storage.main_thread_id(target_session_id) != target_thread_id:
            raise RuntimeError("fork journal target thread 与 target main owner 不匹配")
        self._storage.initialize(
            target_session_id,
            checkpoint_ns,
            thread_id=target_thread_id,
        )
        materialization_id = optional_identity(
            materialization_id, field="materialization_id"
        ) or uuid4().hex
        fork_id = optional_identity(fork_id, field="fork_id") or uuid4().hex
        with (
            self._storage._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._storage._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._storage._require_v2_runtime(connection)
            active = connection.execute(
                "SELECT materialization_id FROM fork_materializations WHERE status IN ('prepared', 'target_committed') LIMIT 1"
            ).fetchone()
            if active is not None:
                active_id = required_text(
                    active[0], field="fork_materializations.materialization_id"
                )
                raise RuntimeError(
                    f"目标 rollout 已存在未完成的 fork 物化: {active_id}"
                )
            meta = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None:
                raise RuntimeError(
                    "database_meta singleton 缺失，不能建立 fork journal"
                )
            rollback_offset = non_negative_int(
                meta[0], field="database_meta.committed_jsonl_offset"
            )
            result = connection.execute(
                "INSERT INTO fork_materializations(materialization_id, fork_id, target_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, status, rollback_jsonl_offset, created_at, source_thread_id, target_thread_id, checkpoint_ns, source_snapshot_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)",
                (
                    materialization_id,
                    fork_id,
                    target_session_id,
                    source_session_id,
                    source_checkpoint_id,
                    source_view_id,
                    fork_mode,
                    relationship,
                    rollback_offset,
                    _now(),
                    source_thread_id,
                    target_thread_id,
                    checkpoint_ns,
                    source_snapshot_sha256,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("fork materialization journal 未写入")
            if fork_mode != "full_rollout_copy":
                prepare_selected_detail_mappings(
                    self, connection, source_session_id=source_session_id,
                    source_thread_id=source_thread_id, target_session_id=target_session_id,
                    target_thread_id=target_thread_id, source_checkpoint_id=source_checkpoint_id,
                    fork_id=fork_id, checkpoint_ns=checkpoint_ns, timestamp=_now(),
                )
            self._storage._active_fork_materializations.add(
                (target_session_id, target_thread_id, checkpoint_ns)
            )
        return materialization_id, fork_id


    def abort_fork_materialization(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """显式回滚尚未提交的 fork；崩溃时由 initialize 执行同一恢复路径。"""
        materialization_id = required_text(
            materialization_id, field="materialization_id"
        )
        target_session_id = required_text(target_session_id, field="target_session_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        target_thread_id = self._storage.main_thread_id(target_session_id)
        with (
            self._storage._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._storage._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._storage._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT status FROM fork_materializations WHERE materialization_id = ? AND target_session_id = ?",
                (materialization_id, target_session_id),
            ).fetchone()
            if row is None:
                return
            status = one_of_text(
                row[0],
                {"prepared", "target_committed", "committed", "aborted"},
                field="fork_materializations.status",
            )
            if status in {"committed", "aborted"}:
                return
            self._recover_fork_materialization(
                target_session_id,
                target_thread_id,
                checkpoint_ns,
                connection,
                self._storage.jsonl_path(
                    target_session_id,
                    checkpoint_ns,
                    thread_id=target_thread_id,
                ),
            )
        self._storage._active_fork_materializations.discard(
            (target_session_id, target_thread_id, checkpoint_ns)
        )


    def complete_fork_materialization(
        self, *, materialization_id: str, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str,
    ) -> str:
        """退出 source snapshot scope 后，仅由目标 journal 完成 retention。"""
        if self._storage._fork_source_snapshot_for_session(target_session_id) is not None:
            raise RuntimeError("fork completion target 不能是 source snapshot")
        with self._storage._connect(target_thread_id, checkpoint_ns,
                           session_id=target_session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT fork_id, source_session_id, source_checkpoint_id, source_view_id, "
                "relationship, status, target_session_id, target_thread_id, checkpoint_ns, "
                "source_snapshot_sha256 FROM fork_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        if row is None or row[6:9] != (target_session_id, target_thread_id, checkpoint_ns):
            raise RuntimeError("fork completion journal target scope 不一致")
        fork_id = required_text(row[0], field="fork_materializations.fork_id")
        source_session_id = required_text(row[1], field="fork_materializations.source_session_id")
        if self._storage._fork_source_snapshot_for_session(source_session_id) is not None:
            raise RuntimeError("fork retention 必须在 source snapshot scope 释放后完成")
        source_checkpoint_id = optional_text(row[2], field="fork_materializations.source_checkpoint_id")
        source_view_id = optional_text(row[3], field="fork_materializations.source_view_id")
        relationship = one_of_text(row[4], {"pinned", "detached"}, field="fork_materializations.relationship")
        status = one_of_text(row[5], {"target_committed", "committed"}, field="fork_materializations.status")
        required_sha256(row[9], field="fork_materializations.source_snapshot_sha256")
        if status == "committed":
            self._storage._active_fork_materializations.discard((target_session_id, target_thread_id, checkpoint_ns))
            return fork_id
        if relationship == "pinned":
            self._retain_fork_source(
                source_session_id=source_session_id,
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                owner_session_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
            )
        with (
            self._storage._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._storage._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            result = connection.execute(
                "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ? AND status = 'target_committed'",
                (_now(), materialization_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork materialization 未进入 committed: {materialization_id}"
                )
        self._storage._active_fork_materializations.discard(
            (target_session_id, target_thread_id, checkpoint_ns)
        )
        return fork_id


    def read_committed_fork_materialization(
        self,
        *,
        target_session_id: str,
        source_session_id: str,
        source_thread_id: str,
        target_thread_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str,
        fork_id: str,
        materialization_id: str,
        source_snapshot_sha256: str,
    ) -> tuple[str, str]:
        """从唯一 committed journal 复核完整 source/target capture 合同。"""
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_id = required_text(fork_id, field="fork_id")
        materialization_id = required_text(
            materialization_id, field="materialization_id"
        )
        source_snapshot_sha256 = required_sha256(
            source_snapshot_sha256, field="source_snapshot_sha256"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("checkpoint_ns 必须是字符串")
        if self._storage.main_thread_id(target_session_id) != target_thread_id:
            raise RuntimeError("fork proof target thread 与 target owner 不一致")
        with self._storage.read_connection(
            target_session_id, target_thread_id, checkpoint_ns,
        ) as connection:
            rows = connection.execute(
                "SELECT materialization_id, fork_id, source_session_id, "
                "target_session_id, source_checkpoint_id, source_view_id, fork_mode, "
                "relationship, status, source_thread_id, target_thread_id, "
                "checkpoint_ns, source_snapshot_sha256 "
                "FROM fork_materializations WHERE target_session_id = ? "
                "AND status = 'committed' ORDER BY created_at, materialization_id",
                (target_session_id,),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    "fork proof 要求 target 只有一条 committed materialization journal: "
                    f"target_session_id={target_session_id}, count={len(rows)}"
                )
            row = rows[0]
            actual = (
                required_text(row[0], field="fork_materializations.materialization_id"),
                required_text(row[1], field="fork_materializations.fork_id"),
                required_text(row[2], field="fork_materializations.source_session_id"),
                required_text(row[3], field="fork_materializations.target_session_id"),
                optional_text(row[4], field="fork_materializations.source_checkpoint_id"),
                optional_text(row[5], field="fork_materializations.source_view_id"),
                one_of_text(
                    row[6],
                    {"context_fork", "history_prefix_fork", "full_rollout_copy"},
                    field="fork_materializations.fork_mode",
                ),
                one_of_text(
                    row[7],
                    {"detached", "pinned"},
                    field="fork_materializations.relationship",
                ),
                one_of_text(
                    row[8], {"committed"}, field="fork_materializations.status"
                ),
                required_text(row[9], field="fork_materializations.source_thread_id"),
                required_text(row[10], field="fork_materializations.target_thread_id"),
                strict_text(
                    row[11],
                    field="fork_materializations.checkpoint_ns",
                    allow_empty=True,
                ),
                required_sha256(
                    row[12], field="fork_materializations.source_snapshot_sha256"
                ),
            )
        expected = (
            materialization_id,
            fork_id,
            source_session_id,
            target_session_id,
            source_checkpoint_id,
            source_view_id,
            fork_mode,
            relationship,
            "committed",
            source_thread_id,
            target_thread_id,
            checkpoint_ns,
            source_snapshot_sha256,
        )
        if actual != expected:
            raise RuntimeError(
                "committed fork journal 与 source/target snapshot 合同不一致: "
                f"expected={expected!r}, actual={actual!r}"
            )
        return actual[0], actual[1]
