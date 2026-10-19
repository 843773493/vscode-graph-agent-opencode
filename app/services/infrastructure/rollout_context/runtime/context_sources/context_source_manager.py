"""上下文来源生命周期协调器的 facade。

具体职责按 owner 拆分到 registry、skill_catalog、lifecycle 与 delta mixin；
本模块只负责公开合同、运行时字段装配和 mixin 组合，保持既有导入路径稳定。
"""

from __future__ import annotations

from collections.abc import Callable

from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
    ContextSourceRebuildState,
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
    PublishedContextSourceRevision,
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
        self._rebuild_transition_id: str | None = None
        self._rebuild_source_batch: PendingContextSourceBatch | None = None
        self._rebuild_control_states: dict[str, ContextSourceControlState] = {}
        self._published_source_revisions: dict[str, PublishedContextSourceRevision] = {}
        self._published_revision_supplier: Callable[
            [tuple[ContextSourceDescriptor, ...]], tuple[PublishedContextSourceRevision, ...]
        ] | None = None
        self.refresh_control_states()

    def restore_committed_baseline(self, source_id: str, read_content: Callable[[str], str]) -> None:
        """只补齐已 applied revision 的内存正文；不改 observation、pending 或控制版本。"""
        state = self._sources[source_id]
        revision = state.latest_visible_committed_revision
        if revision is None or state.latest_visible_committed_content is not None:
            return
        content = read_content(revision)
        if not isinstance(content, str):
            raise TypeError("ContextSourceManager.restore_committed_baseline 正文必须是字符串")
        state.latest_visible_committed_content = content
        if state.latest_revision == revision and state.latest_content is None:
            state.latest_content = content

    def _set_tracking_status(self, state: SourceState, status: str) -> None:
        """由 CSM facade 统一改写来源 tracking 状态。"""
        if status not in {"tracked", "untracked"}:
            raise ValueError(f"非法 tracking_status: {status!r}")
        state.tracking_status = status

    def bind_published_revision_supplier(
        self,
        supplier: Callable[
            [tuple[ContextSourceDescriptor, ...]], tuple[PublishedContextSourceRevision, ...]
        ],
    ) -> None:
        """绑定 source owner 的纯内存冻结出口，CSM 不解析文件或 provider。"""
        self._published_revision_supplier = supplier

    def refresh_context_rebuild_state(
        self,
        rebuild: ContextSourceRebuildState | None = None,
        *,
        checkpoint_ns: str = "",
    ) -> ContextSourceRebuildState | None:
        """对齐已提交的目标 tracking/CAS；只回放控制状态，不恢复正文或消费 epoch。

        替换状态前会把当前 manager 已有的内存正文沿用到已发布版本缓存；正文
        不从 rebuild snapshot 或 source owner 读取，冷启动时没有旧内存正文可沿用。
        """
        owner, port = self._owner, self._control_state_port
        if owner is None or port is None:
            return None
        if rebuild is None:
            rebuild = port.load_context_source_rebuild_state(owner, checkpoint_ns=checkpoint_ns)
        if rebuild is None or rebuild.transition.transition_id == self._rebuild_transition_id:
            return rebuild
        self._pending.clear()
        self._pending_observations.clear()
        for stored in rebuild.states:
            if stored.owner != owner:
                raise RuntimeError("context-source-rebuild-conflict: 目标控制状态 owner 不一致")
            old = self._sources.get(stored.source_id)
            if old is not None and old.latest_revision is not None and old.latest_content is not None:
                self._published_source_revisions[stored.source_id] = PublishedContextSourceRevision(
                    stored.source_id, old.latest_revision, old.latest_content,
                )
            state = SourceState.from_control_state(stored)
            if old is not None and old.descriptor is not None:
                state.bind_descriptor(old.descriptor)
            self._sources[stored.source_id] = state
        self._last_model_call_receipt = None
        self._rebuild_source_batch = None
        self._rebuild_transition_id = rebuild.transition.transition_id
        return rebuild


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
