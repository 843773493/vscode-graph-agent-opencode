"""v2 forks 的显式持久化 owner；只通过注入的基础存储与兄弟 owner 工作。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.fork.lineage.deletion import (
    RolloutForkDeletionMixin,
)
from app.services.infrastructure.rollout_context.fork.lineage.materialization_journal import (
    ForkMaterializationJournalMixin,
)
from app.services.infrastructure.rollout_context.fork.lineage.metadata import (
    ForkMetadataMixin,
)
from app.services.infrastructure.rollout_context.fork.lineage.recovery import (
    RolloutForkRecoveryMixin,
)
from app.services.infrastructure.rollout_context.fork.materialization import (
    ForkMaterializationMixin,
)
from app.services.infrastructure.rollout_context.fork.node_debug_journal import (
    NodeDebugForkJournalMixin,
)
from app.services.infrastructure.rollout_context.fork.retention_release import (
    release_fork_retentions,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class RolloutForkStore(
    ForkMetadataMixin,
    ForkMaterializationMixin,
    ForkMaterializationJournalMixin,
    NodeDebugForkJournalMixin,
    RolloutForkRecoveryMixin,
    RolloutForkDeletionMixin,
):
    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage

    async def release_fork_retentions(self, target_session_id: str, *, target_thread_id: str | None = None, copy_operation_id: str | None = None) -> None:
        await release_fork_retentions(self._storage, self._catalog_store(), target_session_id,
                                      target_thread_id=target_thread_id, copy_operation_id=copy_operation_id)
