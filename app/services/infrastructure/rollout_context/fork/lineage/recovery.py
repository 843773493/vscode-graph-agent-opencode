"""只恢复当前 Thread 的 fork 协调事实；历史业务状态保持原样。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.rollout_context.fork.validation import (
    one_of_text,
    required_sha256,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
)

__all__ = ["RolloutForkRecoveryMixin"]


class RolloutForkRecoveryMixin:
    def _recover_fork_materialization(
        self, session_id: str, thread_id: str, checkpoint_ns: str,
        connection: sqlite3.Connection, jsonl_path: Path,
    ) -> None:
        """prepared 交给冻结创建 intent 重放，已提交副本只收敛本次 journal。

        ``committed_at`` 记录最终 journal/retention 收敛时间；正常完成或恢复都可能写入它。
        """
        if current_scope(connection) != (session_id, thread_id):
            raise RuntimeError("fork recovery 当前存储 owner 不一致")
        rows = connection.execute(
            "SELECT materialization_id,fork_id,source_session_id,relationship,status,"
            "source_snapshot_sha256,fork_mode FROM fork_materializations "
            "WHERE target_session_id=? AND target_thread_id=? AND checkpoint_ns=? "
            "AND status IN ('prepared','target_committed') ORDER BY created_at LIMIT 2",
            (session_id, thread_id, checkpoint_ns),
        ).fetchall()
        if not rows:
            return
        if len(rows) != 1:
            raise RuntimeError("当前目标 Thread 存在多条未完成 fork journal")
        materialization_id, fork_id, source_session_id, relationship, status, digest, mode = rows[0]
        materialization_id = required_text(materialization_id, field="materialization_id")
        fork_id = required_text(fork_id, field="fork_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        relationship = one_of_text(relationship, {"detached", "pinned"}, field="relationship")
        required_sha256(digest, field="source_snapshot_sha256")
        one_of_text(mode, {"last_completed_turn_fork", "view_prefix_fork", "full_rollout_copy"}, field="fork_mode")
        if status == "prepared":
            raise RuntimeError(
                f"fork_materialization_pending: {materialization_id}；必须由冻结创建 intent 恢复"
            )
        self._assert_fork_debug_snapshot_published(connection, materialization_id)
        if relationship == "pinned":
            claim = self._catalog_store().get_fork_retention_claim(fork_id)
            if (claim.source_session_id, claim.target_session_id, claim.state) != (
                source_session_id, session_id, "active",
            ):
                raise RuntimeError("pinned fork recovery 必须先激活原 source claim")
        result = connection.execute(
            "UPDATE fork_materializations SET status='committed',committed_at=?,error_message=NULL "
            "WHERE materialization_id=? AND target_session_id=? AND target_thread_id=? "
            "AND checkpoint_ns=? AND status='target_committed'",
            (utc_now_text(), materialization_id, session_id, thread_id, checkpoint_ns),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"fork recovery 未完成 journal: {materialization_id}")
        connection.commit()
