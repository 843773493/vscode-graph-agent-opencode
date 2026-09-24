"""上下文来源生命周期协调器的 facade。

具体职责按 owner 拆分到 registry、skill_catalog、lifecycle 与 delta mixin；
本模块只负责公开合同、运行时字段装配和 mixin 组合，保持既有导入路径稳定。
"""

from __future__ import annotations

from collections.abc import Callable

from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.delta import (
    SourceDeltaMixin,
    _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.lifecycle import (
    SourceLifecycleMixin,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    CommittedContextSourceBatch,
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceTrackingStateConflict,
    PendingContextSourceBatch,
    PendingSourceObservation,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    SkillCatalogSnapshotConflict,
    SkillLoadAppendStatus,
    SkillLoadMode,
    SkillLoadReceipt,
    SkillLoadStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.registry import (
    SourceRegistryMixin,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    SkillCatalogMixin,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
    SourceState,
)


class ContextSourceManager(
    SourceRegistryMixin,
    SkillCatalogMixin,
    SourceLifecycleMixin,
    SourceDeltaMixin,
):
    """管理已注册 source 的激活状态，不拥有 watcher 或持久化 writer。

    ``owner`` 与 ``control_state_port`` 必须同时提供：owner 是精确
    ``(session_id, thread_id)``，端口是唯一 ContextStore owner 暴露的控制状态
    读写能力。两者省略时 CSM 是纯内存对象（不满足重启恢复合同）。
    """

    def __init__(
        self,
        *,
        owner: ContextSourceOwnerKey | None = None,
        control_state_port: ContextSourceControlStatePort | None = None,
        mutation_intent_port: object | None = None,
        lifecycle_event_sink: Callable[[ContextSourceEvent], None] | None = None,
    ) -> None:
        if (owner is None) != (control_state_port is None):
            raise ValueError(
                "ContextSourceManager 的 owner 与 control_state_port 必须同时提供或同时省略"
            )
        self._owner = owner
        self._control_state_port = control_state_port
        if mutation_intent_port is not None and owner is None:
            raise ValueError(
                "ContextSourceManager 的 mutation_intent_port 需要 owner"
            )
        self._mutation_intent_port = mutation_intent_port
        self._lifecycle_event_sink = lifecycle_event_sink
        self._last_model_call_receipt: CommittedContextSourceBatch | None = None
        self._sources: dict[str, SourceState] = {}
        self._source_ids_by_name: dict[str, str] = {}
        self._pending: dict[str, ContextSourceDelta] = {}
        self._skill_activation_snapshot: SkillCatalogActivationSnapshot | None = None
        self._pending_observations: dict[str, PendingSourceObservation] = {}
        if owner is not None and control_state_port is not None:
            self._restore_control_states(owner, control_state_port)

    def _set_tracking_status(self, state: SourceState, status: str) -> None:
        """由 CSM facade 统一改写来源 tracking 状态。"""
        if status not in {"tracked", "untracked"}:
            raise ValueError(f"非法 tracking_status: {status!r}")
        state.tracking_status = status


__all__ = [
    "CommittedContextSourceBatch",
    "ContextSourceDelta",
    "ContextSourceDescriptor",
    "ContextSourceManager",
    "ContextSourceTrackingStateConflict",
    "PendingContextSourceBatch",
    "PendingSourceObservation",
    "SkillCatalogActivationSnapshot",
    "SkillCatalogBinding",
    "SkillCatalogSnapshotConflict",
    "SkillLoadAppendStatus",
    "SkillLoadMode",
    "SkillLoadReceipt",
    "SkillLoadStatus",
    "_revision",
]
