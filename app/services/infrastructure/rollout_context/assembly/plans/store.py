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
        *,
        plan_id: str,
        seal_idempotency_key: str,
        error_code: str,
        checkpoint_ns: str = "",
    ) -> str:
        """封存事务回滚后独立记录失败，不创建 assembly 或 storage commit。"""
        with self._lock(session_id, checkpoint_ns):
            self.initialize(session_id, checkpoint_ns)
            with self._connect(session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                failure_id = record_seal_failure(
                    connection, session_id, plan_id,
                    seal_idempotency_key=seal_idempotency_key,
                    error_code=error_code,
                )
                self._commit_connection(connection)
                return failure_id

    def create_context_plan(
        self,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        with self._lock(plan.session_id, checkpoint_ns):
            self.initialize(plan.session_id, checkpoint_ns)
            with self._connect(plan.session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                registered = create_registration(connection, plan)
                self._commit_connection(connection)
                return registered

    def revise_context_plan(
        self,
        plan: ContextRequestPlan,
        *,
        expected_revision: int,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        with self._lock(plan.session_id, checkpoint_ns):
            self.initialize(plan.session_id, checkpoint_ns)
            with self._connect(plan.session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                registered = revise_registration(
                    connection, plan, expected_revision=expected_revision
                )
                self._commit_connection(connection)
                return registered

    def get_context_plan_registration(
        self,
        session_id: str,
        *,
        plan_id: str,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        self.initialize(session_id, checkpoint_ns)
        with self._connect(session_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            connection.execute("BEGIN")
            return read_registration(connection, session_id, plan_id)
