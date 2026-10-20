"""向 CSM 暴露 Saver 控制状态端口；持久化归唯一 checkpoint durable owner。"""

from __future__ import annotations

from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlMutation,
    ContextSourceControlState,
    ContextSourceOwnerKey,
)


class ContextSourceControlOwnerMixin:
    """暴露给 CSM 的唯一 owner 端口；实现委托给 RolloutContextStore。"""

    def receive_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        with self._lock:
            self._storage.checkpoints.receive_team_source_observation(session_id, thread_id, ledger_revision=ledger_revision,
                                                          event_hash=event_hash, payload_json=payload_json)

    def require_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        with self._lock:
            self._storage.checkpoints.require_team_source_observation(session_id, thread_id, ledger_revision=ledger_revision,
                                                          event_hash=event_hash, payload_json=payload_json)

    def context_source_control_owner_available(
        self,
        owner: ContextSourceOwnerKey,
    ) -> bool:
        """该 SessionThread 是否有 durable ContextStore owner。

        装配层用它在合成 session（例如工具清单检查）上把 CSM 保持为纯内存
        对象，而不是为不存在的 ContextStore 伪造持久化成功。
        """
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "context_source_control_owner_available 需要 ContextSourceOwnerKey"
            )
        return self._storage.checkpoints.context_source_control_owner_available(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "load_context_source_control_states 需要 ContextSourceOwnerKey"
            )
        return self._storage.checkpoints.read_context_source_control_states(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def apply_context_source_control_mutation(
        self,
        mutation: ContextSourceControlMutation,
    ) -> ContextSourceControlState:
        if not isinstance(mutation, ContextSourceControlMutation):
            raise TypeError(
                "apply_context_source_control_mutation 需要 ContextSourceControlMutation"
            )
        return self._storage.checkpoints.apply_context_source_control_mutation(mutation)

    def apply_source_lifecycle_decision(
        self,
        decision: ApplySourceLifecycleDecision,
        item: CanonicalItemRecord | None,
    ) -> tuple[int, ...]:
        """通过唯一 Saver/ContextStore owner 提交 source intent。"""
        if not isinstance(decision, ApplySourceLifecycleDecision):
            raise TypeError(
                "apply_source_lifecycle_decision 需要 ApplySourceLifecycleDecision"
            )
        return self._storage.checkpoints.apply_source_lifecycle_decision(decision, item)
