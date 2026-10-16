"""通过既有 rollout owner 锁与连接提交 draft registry。"""

from __future__ import annotations

from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    ContextPlanRegistration,
    create_registration,
    read_registration,
    revise_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    record_seal_failure,
)


class ContextPlanRegistryStorageMixin:
    def record_context_plan_seal_failure(
        self,
        session_id: str,
        thread_id: str,
        *,
        plan_id: str,
        seal_idempotency_key: str,
        error_code: str,
        checkpoint_ns: str = "",
    ) -> str:
        """封存事务回滚后独立记录失败，不创建 assembly 或 storage commit。"""
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(
                thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                failure_id = record_seal_failure(
                    connection, session_id, thread_id, plan_id,
                    seal_idempotency_key=seal_idempotency_key,
                    error_code=error_code,
                )
                self._storage._commit_connection(connection)
                return failure_id

    def create_context_plan(
        self,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        with self._storage._lock(
            plan.thread_id, checkpoint_ns, session_id=plan.session_id
        ):
            self._storage.initialize(
                plan.session_id, checkpoint_ns, thread_id=plan.thread_id
            )
            with self._storage._connect(
                plan.thread_id, checkpoint_ns, session_id=plan.session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                registered = create_registration(connection, plan)
                self._storage._commit_connection(connection)
                return registered

    def revise_context_plan(
        self,
        plan: ContextRequestPlan,
        *,
        expected_revision: int,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        with self._storage._lock(
            plan.thread_id, checkpoint_ns, session_id=plan.session_id
        ):
            self._storage.initialize(
                plan.session_id, checkpoint_ns, thread_id=plan.thread_id
            )
            with self._storage._connect(
                plan.thread_id, checkpoint_ns, session_id=plan.session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                registered = revise_registration(
                    connection, plan, expected_revision=expected_revision
                )
                self._storage._commit_connection(connection)
                return registered

    def get_context_plan_registration(
        self,
        session_id: str,
        *,
        plan_id: str,
        checkpoint_ns: str = "",
        thread_id: str,
    ) -> ContextPlanRegistration:
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            self._storage._require_v2_runtime(connection)
            current = connection.execute(
                "SELECT session_id,thread_id FROM context_plans WHERE plan_id=? "
                "AND session_id=? AND thread_id=?", (plan_id, session_id, thread_id),
            ).fetchone()
            if current is None:
                matches = connection.execute(
                    "SELECT session_id,thread_id FROM context_plans WHERE plan_id=? LIMIT 2",
                    (plan_id,),
                ).fetchall()
                if not matches:
                    raise KeyError(f"context plan 不存在: {plan_id}")
                if len(matches) != 1:
                    raise ValueError("source-mismatch: 历史 plan 必须携带唯一精确 scope")
                current = matches[0]
            return read_registration(connection, *current, plan_id)
