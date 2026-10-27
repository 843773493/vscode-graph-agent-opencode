"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

from uuid import uuid4

from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    one_of_text,
    optional_identity,
    optional_text,
    required_sha256,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class ForkMaterializationJournalMixin:
    """fork journal 的 prepare、abort、completion 与 committed proof。"""

    def begin_fork_materialization(
        self, *, target_session_id: str, source_session_id: str,
        source_checkpoint_id: str | None, source_view_id: str | None,
        fork_mode: str, relationship: str, checkpoint_ns: str,
        source_thread_id: str, target_thread_id: str, source_snapshot_sha256: str,
        fork_id: str | None = None, materialization_id: str | None = None,
    ) -> tuple[str, str]:
        """目标 journal 只协调本次 intent，继承的历史 journal 不参与恢复。

        ``created_at`` 是 prepared journal 写入时的记账时间，不表示目标内容提交或最终完成时间。
        """
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        if (target_session_id, target_thread_id) == (source_session_id, source_thread_id):
            raise ValueError("fork 不能覆盖来源 Thread")
        source_checkpoint_id = optional_text(source_checkpoint_id, field="source_checkpoint_id")
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_mode = one_of_text(
            fork_mode, {"last_completed_turn_fork", "view_prefix_fork", "full_rollout_copy"},
            field="fork_mode",
        )
        relationship = one_of_text(relationship, {"detached", "pinned"}, field="relationship")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        source_snapshot_sha256 = required_sha256(source_snapshot_sha256, field="source_snapshot_sha256")
        materialization_id = optional_identity(materialization_id, field="materialization_id") or uuid4().hex
        fork_id = optional_identity(fork_id, field="fork_id") or uuid4().hex
        owner = (target_session_id, target_thread_id, checkpoint_ns)
        expected = (
            fork_id, target_session_id, source_session_id, source_checkpoint_id,
            source_view_id, fork_mode, relationship, source_thread_id,
            target_thread_id, checkpoint_ns, source_snapshot_sha256,
        )
        self._storage._active_fork_materializations.add(owner)
        try:
            self._storage.initialize(target_session_id, checkpoint_ns, thread_id=target_thread_id)
            with (
                self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
                self._storage._connect(target_thread_id, checkpoint_ns, session_id=target_session_id) as connection,
            ):
                self._storage._require_v2_runtime(connection)
                if current_scope(connection) != (target_session_id, target_thread_id):
                    raise RuntimeError("fork journal 当前存储 owner 不一致")
                existing = connection.execute(
                    "SELECT fork_id,target_session_id,source_session_id,source_checkpoint_id,"
                    "source_view_id,fork_mode,relationship,source_thread_id,target_thread_id,"
                    "checkpoint_ns,source_snapshot_sha256,status FROM fork_materializations "
                    "WHERE materialization_id = ?", (materialization_id,),
                ).fetchone()
                if existing is not None:
                    if existing[:-1] != expected or existing[-1] not in {"prepared", "target_committed", "committed"}:
                        raise RuntimeError("fork journal 与冻结 operation 合同不一致")
                    return materialization_id, fork_id
                active = connection.execute(
                    "SELECT materialization_id FROM fork_materializations "
                    "WHERE target_session_id=? AND target_thread_id=? AND checkpoint_ns=? "
                    "AND status IN ('prepared','target_committed') LIMIT 1", owner,
                ).fetchone()
                if active is not None:
                    raise RuntimeError(f"目标 Thread 已存在未完成的 fork 物化: {active[0]}")
                if connection.execute("SELECT 1 FROM item_catalog LIMIT 1").fetchone() is not None or (
                    connection.execute("SELECT 1 FROM checkpoints LIMIT 1").fetchone() is not None
                ):
                    raise ValueError("fork target Thread 必须是本次创建的空目标")
                meta = connection.execute(
                    "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id=1",
                ).fetchone()
                if meta is None:
                    raise RuntimeError("database_meta singleton 缺失，不能建立 fork journal")
                rollback_offset = non_negative_int(meta[0], field="database_meta.committed_jsonl_offset")
                result = connection.execute(
                    "INSERT INTO fork_materializations(materialization_id,fork_id,target_session_id,"
                    "source_session_id,source_checkpoint_id,source_view_id,fork_mode,relationship,status,"
                    "rollback_jsonl_offset,created_at,source_thread_id,target_thread_id,checkpoint_ns,"
                    "source_snapshot_sha256) VALUES (?,?,?,?,?,?,?,?,'prepared',?,?,?,?,?,?)",
                    (materialization_id, *expected[:7], rollback_offset, _now(), *expected[7:]),
                )
                if result.rowcount != 1:
                    raise RuntimeError("fork materialization journal 未写入")
            return materialization_id, fork_id
        except BaseException:
            self._storage._active_fork_materializations.discard(owner)
            raise

    def abort_fork_materialization(
        self, materialization_id: str, *, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str = "",
    ) -> None:
        """只终结精确目标的 prepared operation，物理清理由创建 owner 执行。"""
        materialization_id = required_text(materialization_id, field="materialization_id")
        target_session_id = required_text(target_session_id, field="target_session_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        owner = (target_session_id, target_thread_id, checkpoint_ns)
        with (
            self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
            self._storage._connect(target_thread_id, checkpoint_ns, session_id=target_session_id) as connection,
        ):
            if current_scope(connection) != (target_session_id, target_thread_id):
                raise RuntimeError("fork abort 当前存储 owner 不一致")
            row = connection.execute(
                "SELECT status FROM fork_materializations WHERE materialization_id=? "
                "AND target_session_id=? AND target_thread_id=? AND checkpoint_ns=?",
                (materialization_id, *owner),
            ).fetchone()
            if row is not None:
                if row[0] not in {"prepared", "aborted"}:
                    raise RuntimeError("已提交 fork 必须由创建 owner 终结，不能回滚历史事实")
                connection.execute(
                    "UPDATE fork_materializations SET status='aborted',error_message=? "
                    "WHERE materialization_id=? AND target_session_id=? AND target_thread_id=? "
                    "AND checkpoint_ns=? AND status='prepared'",
                    ("fork 创建已终止", materialization_id, *owner),
                )
        self._storage._active_fork_materializations.discard(owner)


    def complete_fork_materialization(
        self, *, materialization_id: str, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str,
    ) -> str:
        """退出 source snapshot scope 后，仅由目标 journal 完成 retention。"""
        if self._storage.fork_paths.source_snapshot(target_session_id) is not None:
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
        if self._storage.fork_paths.source_snapshot(source_session_id) is not None:
            raise RuntimeError("fork retention 必须在 source snapshot scope 释放后完成")
        optional_text(row[2], field="fork_materializations.source_checkpoint_id")
        optional_text(row[3], field="fork_materializations.source_view_id")
        relationship = one_of_text(row[4], {"pinned", "detached"}, field="fork_materializations.relationship")
        status = one_of_text(row[5], {"target_committed", "committed"}, field="fork_materializations.status")
        required_sha256(row[9], field="fork_materializations.source_snapshot_sha256")
        if status == "committed":
            self._storage._active_fork_materializations.discard((target_session_id, target_thread_id, checkpoint_ns))
            return fork_id
        if relationship == "pinned":
            claim = self._catalog_store().get_fork_retention_claim(fork_id)
            if (claim.source_session_id, claim.target_session_id, claim.state) != (
                source_session_id, target_session_id, "active",
            ):
                raise RuntimeError("pinned fork completion 必须先由异步 owner 激活原 source claim")
        with (
            self._storage._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._storage._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._assert_fork_debug_snapshot_published(connection, materialization_id)
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
        with self._storage.read_connection(
            target_session_id, target_thread_id, checkpoint_ns,
        ) as connection:
            rows = connection.execute(
                "SELECT materialization_id, fork_id, source_session_id, "
                "target_session_id, source_checkpoint_id, source_view_id, fork_mode, "
                "relationship, status, source_thread_id, target_thread_id, "
                "checkpoint_ns, source_snapshot_sha256 "
                "FROM fork_materializations WHERE target_session_id = ? "
                "AND target_thread_id=? AND checkpoint_ns=? AND materialization_id=? "
                "AND status='committed' LIMIT 2",
                (target_session_id, target_thread_id, checkpoint_ns, materialization_id),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    "fork proof 要求 target 只有一条 committed materialization journal: "
                    f"target_session_id={target_session_id}, count={len(rows)}"
                )
            row = rows[0]
            self._assert_fork_debug_snapshot_published(connection, materialization_id)
            actual = (
                required_text(row[0], field="fork_materializations.materialization_id"),
                required_text(row[1], field="fork_materializations.fork_id"),
                required_text(row[2], field="fork_materializations.source_session_id"),
                required_text(row[3], field="fork_materializations.target_session_id"),
                optional_text(row[4], field="fork_materializations.source_checkpoint_id"),
                optional_text(row[5], field="fork_materializations.source_view_id"),
                one_of_text(
                    row[6],
                    {"last_completed_turn_fork", "view_prefix_fork", "full_rollout_copy"},
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
