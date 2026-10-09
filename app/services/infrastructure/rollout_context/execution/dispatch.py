"""Turn execution 的 dispatch 查询边界。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.enums import TurnStatus
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class RolloutExecutionDispatchMixin:
    """只查询已经存在且仍可派发的 execution。"""

    def execution_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str | None = None,
        checkpoint_ns: str = "",
        writer_connection: sqlite3.Connection | None = None,
    ) -> str:
        """返回可 dispatch 的 Turn execution，供 provider adapter 建立关联。

        这是 provider dispatch 的恢复边界，而不是一个任意的 execution
        查询器。terminal Turn 不能被旧 API 重新派发；显式
        ``resume_turn``/``replay_as_new_turn`` 必须先创建新的
        active Turn/execution。
        """
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        turn_id = strict_text(turn_id, field="turn_id")
        with self._storage.read_connection(session_id, owner_thread_id, checkpoint_ns, connection=writer_connection) as connection:
            self._storage._require_v2_runtime(connection)
            row = connection.execute(
                """
                SELECT tr.status, e.execution_id
                FROM turn_records AS tr
                JOIN executions AS e ON e.session_id = tr.session_id
                    AND e.thread_id = tr.thread_id
                    AND e.execution_id = COALESCE(
                    tr.last_execution_id, tr.initial_execution_id
                )
                WHERE tr.session_id = ? AND tr.thread_id = ? AND tr.turn_id = ?
                ORDER BY e.attempt DESC, e.execution_ordinal DESC
                LIMIT 1
                """,
                (session_id, owner_thread_id, turn_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Turn 没有 execution: {turn_id}")
        status = strict_text(row[0], field=f"turn_records.status: {turn_id}")
        if status not in {TurnStatus.OPEN.value, TurnStatus.ACTIVE.value}:
            raise ValueError(f"turn_not_resumable: {turn_id} status={status}")
        return strict_text(row[1], field=f"executions.execution_id: {turn_id}")

    def dispatch_replay(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> str:
        """返回绑定原 Turn 的 dispatch execution，不创建新 Turn。"""
        return self.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )


__all__ = ["RolloutExecutionDispatchMixin"]
