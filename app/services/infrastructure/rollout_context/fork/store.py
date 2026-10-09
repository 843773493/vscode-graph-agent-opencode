"""v2 forks 的显式持久化 owner；只通过注入的基础存储与兄弟 owner 工作。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.fork.completion import (
    ForkCompletionMixin,
)
from app.services.infrastructure.rollout_context.fork.copy_items import (
    ForkItemCopyMixin,
)
from app.services.infrastructure.rollout_context.fork.identity_mapping import (
    ForkIdentityMappingMixin,
)
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
from app.services.infrastructure.rollout_context.fork.overlay_copy import (
    ForkOverlayCopyMixin,
)
from app.services.infrastructure.rollout_context.fork.remap import ForkRemapMixin

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class RolloutForkStore(
    ForkIdentityMappingMixin,
    ForkMetadataMixin,
    ForkRemapMixin,
    ForkMaterializationMixin,
    ForkMaterializationJournalMixin,
    NodeDebugForkJournalMixin,
    ForkOverlayCopyMixin,
    ForkCompletionMixin,
    ForkItemCopyMixin,
    RolloutForkRecoveryMixin,
    RolloutForkDeletionMixin,
):
    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage
