"""v2 checkpoints 的显式持久化 owner；只通过注入的基础存储与兄弟 owner 工作。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    RolloutCompactionPreflightOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.fork_boundary import (
    RolloutForkBoundaryOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.active_view import (
    RolloutActiveViewMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.context_source_control import (
    ContextSourceControlStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.context_source_versions import (
    ContextSourceVersionsStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.operations import (
    RolloutCheckpointOperationsMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.persistence import (
    RolloutCheckpointPersistenceMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.queries import (
    RolloutCheckpointQueriesMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.view_anchor import (
    RolloutViewAnchorMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.writes import (
    RolloutWriteMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.indexed_records import (
    IndexedRecordQueryMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_materializer import (
    RolloutMessageMaterializerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_projections import (
    RolloutMessageProjectionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_view import (
    RolloutMessageViewMixin,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


from app.services.infrastructure.rollout_context.checkpoint.durable.source_activation import (
    ContextSourceActivationMixin,
)


class RolloutCheckpointStore(
    RolloutCheckpointPersistenceMixin,
    RolloutViewAnchorMixin,
    RolloutCheckpointOperationsMixin,
    RolloutCompactionPreflightOwnerMixin,
    RolloutForkBoundaryOwnerMixin,
    RolloutCheckpointQueriesMixin,
    RolloutMessageViewMixin,
    RolloutMessageMaterializerMixin,
    IndexedRecordQueryMixin,
    RolloutWriteMixin,
    ContextSourceControlStorageMixin,
    ContextSourceActivationMixin,
    ContextSourceVersionsStorageMixin,
    RolloutMessageProjectionMixin,
    RolloutActiveViewMixin,
):
    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage
