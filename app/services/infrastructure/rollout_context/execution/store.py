"""v2 executions 的显式持久化 owner；只通过注入的基础存储与兄弟 owner 工作。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.execution.admissions import (
    RolloutExecutionAdmissionMixin,
)
from app.services.infrastructure.rollout_context.execution.dispatch import (
    RolloutExecutionDispatchMixin,
)
from app.services.infrastructure.rollout_context.execution.executions import (
    RolloutExecutionsMixin,
)
from app.services.infrastructure.rollout_context.execution.internal_convergence import (
    RolloutInternalConvergenceMixin,
)
from app.services.infrastructure.rollout_context.execution.item_commits import (
    CanonicalItemCommitMixin,
)
from app.services.infrastructure.rollout_context.execution.lifecycle import (
    RolloutTurnLifecycleMixin,
)
from app.services.infrastructure.rollout_context.execution.model_calls import (
    RolloutModelCallsMixin,
)
from app.services.infrastructure.rollout_context.execution.recovery import (
    RolloutExecutionRecoveryMixin,
)
from app.services.infrastructure.rollout_context.execution.replay import (
    RolloutReplayMixin,
)
from app.services.infrastructure.rollout_context.execution.runtime_wakeup import (
    RuntimeWakeupStorageMixin,
)
from app.services.infrastructure.rollout_context.execution.terminal_helpers import (
    RolloutTerminalProjectionMixin,
)
from app.services.infrastructure.rollout_context.execution.turn_index import (
    RolloutTurnIndexMixin,
)
from app.services.infrastructure.rollout_context.execution.turns import (
    RolloutTurnsMixin,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class RolloutExecutionStore(
    RuntimeWakeupStorageMixin,
    RolloutTurnsMixin,
    RolloutReplayMixin,
    RolloutTurnIndexMixin,
    RolloutExecutionsMixin,
    RolloutExecutionAdmissionMixin,
    RolloutExecutionDispatchMixin,
    RolloutExecutionRecoveryMixin,
    RolloutTerminalProjectionMixin,
    RolloutInternalConvergenceMixin,
    RolloutModelCallsMixin,
    RolloutTurnLifecycleMixin,
    CanonicalItemCommitMixin,
):
    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage
