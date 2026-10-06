"""把已提交 view rebuild 的 typed 状态暴露给唯一 CSM/prepare owner。"""

from __future__ import annotations

from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    PendingPrefixEpochTransition,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_source_control import (
    _state_from_row,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
    ContextSourceRebuildState,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class ContextEpochTransitionOwnerMixin:
    """没有独立 applied writer；pending 与 applied assembly 都来自 thread rollout。"""

    def load_context_source_rebuild_state(
        self,
        owner: ContextSourceOwnerKey,
        *,
        checkpoint_ns: str = "",
    ) -> ContextSourceRebuildState | None:
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError("load_context_source_rebuild_state 需要精确 ContextSourceOwnerKey")
        rows = self._storage.read_context_source_rebuild_rows(
            owner.session_id, owner.thread_id, checkpoint_ns=checkpoint_ns,
        )
        if rows is None:
            return None
        pending, states, source_assembly, retained_refs = rows
        return ContextSourceRebuildState(
            transition=PendingPrefixEpochTransition(
                transition_id=strict_text(pending[0], field="transition_id"),
                reason=EpochReason(strict_text(pending[1], field="transition.reason")),
            ),
            states=tuple(_state_from_row(row) for row in states),
            source_assembly=source_assembly,
            retained_source_refs=retained_refs,
        )


__all__ = ["ContextEpochTransitionOwnerMixin"]
