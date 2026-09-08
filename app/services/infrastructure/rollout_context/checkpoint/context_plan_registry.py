"""Saver 对 draft plan 生命周期的唯一公共入口。"""

from __future__ import annotations

from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    runtime_draft,
)


class ContextPlanRegistryOwnerMixin:
    def create_context_plan(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        """提交无正文草稿；重复 creation key 返回同一份实际持久 identity。"""
        self._require_context_plan_owner(session_id, plan)
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        self._find_runtime_context_plan(session_id, plan.plan_id, checkpoint_ns)
        registered = self._storage.create_context_plan(
            plan, checkpoint_ns=checkpoint_ns
        )
        runtime_draft(registered)
        self._cache_draft_bodies(session_id, checkpoint_ns, plan, registered)
        return registered

    def revise_context_plan(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        expected_revision: int,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        """仅以显式 revision 修订尚未 sealed 的 registry，不覆盖创建幂等 preimage。"""
        self._require_context_plan_owner(session_id, plan)
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        runtime_draft(
            self.get_context_plan_registration(
                session_id, plan_id=plan.plan_id, checkpoint_ns=checkpoint_ns
            )
        )
        registered = self._storage.revise_context_plan(
            plan,
            expected_revision=expected_revision,
            checkpoint_ns=checkpoint_ns,
        )
        runtime_draft(registered)
        self._cache_draft_bodies(session_id, checkpoint_ns, plan, registered)
        return registered

    def get_context_plan_registration(
        self,
        session_id: str,
        *,
        plan_id: str,
        checkpoint_ns: str = "",
    ) -> ContextPlanRegistration:
        return self._storage.get_context_plan_registration(
            session_id,
            plan_id=plan_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def _cache_draft_bodies(
        self,
        session_id: str,
        checkpoint_ns: str,
        supplied: ContextRequestPlan,
        registered: ContextPlanRegistration,
    ) -> None:
        # 重试 create 可能返回已修订的 draft；不能把原请求的旧正文塞入当前来源缓存。
        draft = runtime_draft(registered)
        if registered.plan_state != "unsealed":
            return
        supplied_by_id = {item.contribution_id: item for item in supplied.contributions}
        with self._lock:
            for item in draft.contributions:
                candidate = supplied_by_id.get(item.contribution_id)
                if candidate is None or candidate.body is None:
                    continue
                if (
                    candidate.source_revision,
                    candidate.content_hash,
                    candidate.redacted_stable_digest,
                ) != (
                    item.source_revision,
                    item.content_hash,
                    item.redacted_stable_digest,
                ):
                    continue
                self._plan_request_content[
                    (
                        session_id,
                        checkpoint_ns,
                        draft.plan_id,
                        item.contribution_id,
                    )
                ] = candidate.body

    def _find_runtime_context_plan(
        self, session_id: str, plan_id: str, checkpoint_ns: str
    ) -> ContextPlanRegistration | None:
        """只把不存在视为可显式创建；import/corruption 必须保留原错误。"""
        try:
            registered = self.get_context_plan_registration(
                session_id, plan_id=plan_id, checkpoint_ns=checkpoint_ns
            )
        except KeyError:
            return None
        runtime_draft(registered)
        return registered
