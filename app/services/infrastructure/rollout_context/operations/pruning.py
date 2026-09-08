"""v2 checkpoint/view pruning owner。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutPruningCandidate,
        RolloutPruningPlan,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RolloutPruningMixin:
    """按 active checkpoint/view lineage 规划并执行逻辑裁剪。"""

    def plan_pruning(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        retain_checkpoint_ids: Iterable[str] = (),
        audit_before_sequence: int | None = None,
    ) -> RolloutPruningPlan:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            RolloutPruningCandidate,
            RolloutPruningPlan,
        )

        self.initialize(thread_id, checkpoint_ns)
        retained = tuple(dict.fromkeys(retain_checkpoint_ids))
        with self._connect(thread_id, checkpoint_ns) as connection:
            self._require_v2_runtime(connection)
            meta = connection.execute(
                "SELECT active_branch_id, projection_epoch, last_message_sequence FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None:
                raise RuntimeError("rollout database_meta 缺失")
            active_branch_id, _projection_epoch = self._namespace_state(
                connection, checkpoint_ns
            )
            query = """
                SELECT c.checkpoint_id, c.view_id, c.commit_id
                FROM checkpoints c
                WHERE c.checkpoint_ns = ? AND c.status = 'active'
                  AND c.checkpoint_id NOT IN (
                      SELECT head_checkpoint_id FROM branches
                      WHERE branch_id = ? AND status = 'active' AND head_checkpoint_id IS NOT NULL
                  )
            """
            params: list[object] = [checkpoint_ns, active_branch_id]
            if retained:
                query += (
                    " AND c.checkpoint_id NOT IN ("
                    + ",".join("?" for _ in retained)
                    + ")"
                )
                params.extend(retained)
            if audit_before_sequence is not None:
                query += " AND c.commit_id < ?"
                params.append(audit_before_sequence)
            query += " ORDER BY c.commit_id"
            rows = connection.execute(query, tuple(params)).fetchall()
            candidates: list[RolloutPruningCandidate] = []
            for checkpoint_id, view_id, _commit_id in rows:
                protected = connection.execute(
                    """
                    SELECT 1 FROM retention_refs
                    WHERE status = 'active' AND (target_view_id = ? OR reference_id = ?)
                    UNION ALL
                    SELECT 1 FROM fork_origins
                    WHERE relationship = 'pinned' AND (source_view_id = ? OR source_checkpoint_id = ?)
                    LIMIT 1
                    """,
                    (view_id, checkpoint_id, view_id, checkpoint_id),
                ).fetchone()
                if protected is not None:
                    continue
                candidates.append(
                    RolloutPruningCandidate(
                        checkpoint_id=str(checkpoint_id),
                        view_id=str(view_id),
                        reason="unreferenced_checkpoint",
                    )
                )
        return RolloutPruningPlan(
            self.rollout_id(thread_id, checkpoint_ns),
            self.initialize(thread_id, checkpoint_ns).committed_sequence,
            tuple(candidates),
        )

    def execute_pruning(
        self, thread_id: str, plan: RolloutPruningPlan, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        current = self.initialize(thread_id, checkpoint_ns)
        if (
            current.rollout_id != plan.rollout_id
            or current.committed_sequence != plan.committed_sequence
        ):
            raise RuntimeError("pruning plan 不属于当前 rollout 水位")
        if not plan.candidates:
            return ()
        with (
            self._lock(thread_id, checkpoint_ns),
            self._connect(thread_id, checkpoint_ns) as connection,
        ):
            self._require_v2_runtime(connection)
            transaction_id = uuid4().hex
            timestamp = _now()
            connection.execute("BEGIN IMMEDIATE")
            for candidate in plan.candidates:
                row = connection.execute(
                    "SELECT status, view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (candidate.checkpoint_id, checkpoint_ns),
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        f"pruning checkpoint 不存在: {candidate.checkpoint_id}"
                    )
                if row[0] != "active" or row[1] != candidate.view_id:
                    raise RuntimeError(
                        f"pruning checkpoint 状态或 view 已变化: {candidate.checkpoint_id}"
                    )
                connection.execute(
                    "UPDATE checkpoints SET status = 'pruned' WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (candidate.checkpoint_id, checkpoint_ns),
                )
                self._insert_control(
                    connection,
                    "prune_marked",
                    "checkpoint",
                    candidate.checkpoint_id,
                    None,
                    candidate.view_id,
                    candidate.checkpoint_id,
                    {"reason": candidate.reason, "physical_jsonl": False},
                    transaction_id,
                    timestamp,
                )
            connection.execute(
                "UPDATE checkpoint_namespace_state SET projection_epoch = projection_epoch + 1, updated_at = ? WHERE checkpoint_ns = ?",
                (timestamp, checkpoint_ns),
            )
            connection.execute(
                "UPDATE database_meta SET history_view_revision = history_view_revision + 1, updated_at = ? WHERE singleton_id = 1",
                (timestamp,),
            )
            connection.commit()
        return tuple(candidate.checkpoint_id for candidate in plan.candidates)
