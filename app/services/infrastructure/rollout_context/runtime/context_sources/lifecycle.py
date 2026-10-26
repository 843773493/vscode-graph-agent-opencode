"""Context source observation、pending 与 durable commit 生命周期。"""

from __future__ import annotations

from dataclasses import replace
from types import EllipsisType

from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
        ContextSourceRebuildState,
        ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.delta import (
        _adopt_restored_baseline,
        _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        CommittedContextSourceBatch,
        ContextSourceDelta,
        ContextSourceSnapshot,
        PendingContextSourceBatch,
        PendingSourceObservation,
        PublishedContextSourceRevision,
        SourceObservationResult,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
        SourceState,
)


class SourceLifecycleMixin:
        _pending: dict[str, ContextSourceDelta]

        def observe(self, source_id: str, content: str, *, revision: str | None = None) -> SourceObservationResult:
            """接收已登记来源快照；处理结果与待封存状态分别返回，不推进 applied。"""
            self.refresh_context_rebuild_state()
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if not isinstance(content, str):
                raise TypeError("ContextSourceManager.observe.content 必须是字符串")
            if not state.tracked:
                return SourceObservationResult(source_id, "not_tracked", source_id in self._pending)
            if revision is not None and (not isinstance(revision, str) or not revision):
                raise ValueError("ContextSourceManager.observe.revision 必须是非空字符串或 None")
            effective_revision = _revision(content) if revision is None else revision
            unchanged = state.latest_revision == effective_revision and state.latest_content == content
            # 重启恢复：已提交的同 revision 只重建 diff 基准。
            restored = _adopt_restored_baseline(state, content, effective_revision)
            if not unchanged and not restored:
                self._accept_source_observation(state, content, effective_revision)
            self._published_source_revisions[source_id] = PublishedContextSourceRevision(
                source_id, effective_revision, content,
            )
            pending = source_id in self._pending
            if restored or (
                not unchanged and not pending
                and effective_revision == state.latest_visible_committed_revision
            ):
                return SourceObservationResult(source_id, "baseline_restored", pending)
            return SourceObservationResult(source_id, "unchanged" if unchanged else "accepted", pending)

        def _accept_source_observation(
            self, state: SourceState, content: str, revision: str,
        ) -> None:
            """普通观察与 Skill 激活共用候选提交；失败不发布正文、provenance 或 delta。"""
            candidate = self._source_observation_candidate(state, content, revision)
            self._persist_control_state(candidate, operation="observe")
            self._sources[state.source_id] = candidate
            self._queue_delta(candidate)

        def _source_observation_candidate(
            self, state: SourceState, content: str, revision: str,
        ) -> SourceState:
            """从已提交基准准备观察候选，不改 registration、队列或发布缓存。"""
            observed = list(state.observed_revisions)
            if revision not in observed:
                observed.append(revision)
            return replace(
                state, latest_revision=revision, latest_content=content,
                pending_kind="activation" if state.latest_visible_committed_revision is None else "delta",
                observed_revisions=observed,
            )

        def mark_pending_observation(
            self,
            source_id: str,
            revision: str | None,
            *,
            reconcile: bool = False,
        ) -> bool:
            """把「来源有新 revision / 需要重新对账」标记为待观察，不读取任何内容。

            资源事件接线只调用本方法：事件回调保持零 I/O，正文仍由来源 owner 的
            内存快照在 :meth:`next_pending_observation` 之后提供。尚未 tracked 的
            来源也会被登记，保证首次 activation 与后续 delta 走同一条事件驱动路径；
            消费者只返回当时仍然 tracked 的来源。

            ``reconcile=True`` 表示通知丢失或无法归属到单一 revision，必须按权威
            快照重新对账；此时允许 ``revision=None``（来源尚未有过已知 revision）。
            同一来源同时只保留一个待观察标记，重复标记不会堆积。
            """
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if revision is not None and (not isinstance(revision, str) or not revision):
                raise ValueError(
                    "mark_pending_observation.revision 必须是非空字符串或 None"
                )
            if not reconcile:
                if revision is None:
                    raise ValueError(
                        "mark_pending_observation 非 reconcile 标记必须提供 revision"
                    )
                if revision in {state.latest_revision, state.latest_visible_committed_revision}:
                    return False
            marker = PendingSourceObservation(
                source_id=source_id,
                revision=None if reconcile else revision,
            )
            if self._pending_observations.get(source_id) == marker:
                return False
            self._pending_observations[source_id] = marker
            return True

        def mark_all_pending_observations(
            self,
            source_ids: tuple[str, ...] | None = None,
        ) -> tuple[str, ...]:
            """把指定（默认全部）已注册来源标记为「需要按权威快照重新对账」。

            用于资源通知丢失（gap）后的全量 reconcile：只登记来源 identity，不读取
            正文、不要求已知 revision；返回本次新标记的 source_id。
            """
            candidates = tuple(self._sources) if source_ids is None else source_ids
            marked: list[str] = []
            for source_id in candidates:
                if source_id not in self._sources:
                    raise KeyError(f"Context source 不存在: source_id={source_id}")
                if self.mark_pending_observation(source_id, None, reconcile=True):
                    marked.append(source_id)
            return tuple(marked)

        def next_pending_observation(self) -> PendingSourceObservation | None:
            """取出一条最早的待观察标记；未跟踪来源的标记在这里被丢弃。"""
            while self._pending_observations:
                source_id, pending = next(iter(self._pending_observations.items()))
                del self._pending_observations[source_id]
                state = self._sources.get(source_id)
                if state is None:
                    raise KeyError(f"Context source 不存在: source_id={source_id}")
                if not state.tracked:
                    continue
                return pending
            return None

        def pending_observation_count(self) -> int:
            return len(self._pending_observations)

        def source_observation_state(
            self,
            source_id: str,
        ) -> tuple[ContextSourceTrackingStatus, str | None]:
            """返回来源的 tracking 状态与最新已知 revision，供 owner 对账。"""
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            return state.tracking_status, state.latest_revision

        def source_id_for_resource_uri(self, resource_uri: str) -> str | None:
            """按来源虚拟 identity 反查 source_id；locator/物理路径不参与匹配。"""
            if not isinstance(resource_uri, str) or not resource_uri:
                raise ValueError("source_id_for_resource_uri 需要非空 resource_uri")
            for state in self._sources.values():
                descriptor = state.descriptor
                if descriptor is not None and descriptor.resource_uri == resource_uri:
                    return state.source_id
            return None

        def prepare_rebuild_sources(
            self,
            rebuild: ContextSourceRebuildState,
        ) -> PendingContextSourceBatch:
            """从来源 owner 冻结的已发布正文准备 tracked 完整状态；不写 durable applied。"""
            self.refresh_context_rebuild_state(rebuild)
            if self._rebuild_source_batch is not None:
                return self._rebuild_source_batch
            self._rebuild_control_states = {state.source_id: state for state in rebuild.states}
            # 本次显式 snapshot 仍是待封存输入；只排除已停止追踪的历史正文，
            # 不能把 transition 建立后新接受的激活一起丢掉。
            states = tuple(
                state for state in self._sources.values()
                if state.tracked or state.source_id in self._pending
            )
            if any(state.descriptor is None for state in states):
                raise RuntimeError("tracked-source-unavailable: 目标 registration 缺少 source owner binding")
            supplier = self._published_revision_supplier
            resource_descriptors = tuple(
                state.descriptor for state in states
                if state.tracked and state.descriptor.resource_uri is not None
            )
            frozen_resources = () if supplier is None else supplier(resource_descriptors)
            published = {source.source_id: source for source in frozen_resources}
            if len(published) != len(frozen_resources):
                raise RuntimeError("context-source-rebuild-conflict: published snapshot 重复 source identity")
            sources, deltas = [], []
            for state in states:
                descriptor = state.descriptor
                if not state.tracked:
                    if state.latest_revision is None or state.latest_content is None:
                        raise RuntimeError("context-source-rebuild-conflict: 显式激活缺少冻结正文")
                    sources.append(ContextSourceSnapshot(
                        descriptor=descriptor, revision=state.latest_revision,
                        content=state.latest_content,
                        applied_revision=state.latest_visible_committed_revision, tracked=False,
                    ))
                    deltas.append(self._pending[state.source_id])
                    continue
                frozen = published.get(state.source_id)
                if frozen is None and (supplier is None or descriptor.resource_uri is None):
                    if descriptor.source_kind == "skill":
                        snapshot = self._skill_activation_snapshot
                        binding = (
                            snapshot.sources.get(descriptor.resource_uri)
                            if snapshot is not None and descriptor.resource_uri is not None
                            else snapshot.entries.get(state.name) if snapshot is not None else None
                        )
                        if binding is not None and (
                            descriptor.resource_uri is None or descriptor.resource_uri == binding.display_uri
                        ):
                            frozen = PublishedContextSourceRevision(
                                state.source_id, binding.activation_revision, binding.body,
                            )
                    elif descriptor.resource_uri is None:
                        frozen = self._published_source_revisions.get(state.source_id)
                if frozen is None:
                    raise RuntimeError("tracked-source-unavailable: 已发布快照缺少 tracked source: " + state.source_id)
                if not isinstance(frozen, PublishedContextSourceRevision) or frozen.source_id != state.source_id:
                    raise RuntimeError("context-source-rebuild-conflict: published snapshot source identity 不一致")
                sources.append(ContextSourceSnapshot(
                    descriptor=descriptor, revision=frozen.revision, content=frozen.content,
                    applied_revision=state.latest_visible_committed_revision, tracked=True,
                    expected_control_revision=state.persisted_state_revision,
                ))
                if frozen.revision != state.latest_visible_committed_revision:
                    deltas.append(ContextSourceDelta(
                        source_id=state.source_id, source_name=state.name, source_kind=state.source_kind,
                        revision=frozen.revision, previous_revision=state.latest_visible_committed_revision,
                        content=frozen.content, content_hash=_revision(frozen.content), kind="rebuild",
                    ))
            batch = PendingContextSourceBatch(deltas=tuple(deltas), sources=tuple(sources))
            # 全部来源成功冻结后才安装候选正文，失败不能留下局部内存推进。
            for source in batch.sources:
                state = self._sources[source.descriptor.source_id]
                state.latest_revision = source.revision
                state.latest_content = source.content
            self._rebuild_source_batch = batch
            return batch

        def prepare_sources(
            self,
            rebuild: ContextSourceRebuildState | None | EllipsisType = ...,
        ) -> PendingContextSourceBatch:
            """冻结完整有效来源与最终 delta；不写 applied 或消费任何 intent。"""
            current_rebuild: ContextSourceRebuildState | None = self.refresh_context_rebuild_state(rebuild)
            if current_rebuild is not None:
                return self.prepare_rebuild_sources(current_rebuild)
            sources = []
            for state in self._sources.values():
                revision, content = state.latest_revision, state.latest_content
                if not state.tracked and state.source_id not in self._pending:
                    # 停止追踪后的自动候选不是有效上下文；后续 ToolSet/root
                    # 重建只能携带已经封存的正文，不能重新激活被显式替换的更新。
                    revision, content = state.latest_visible_committed_revision, state.latest_visible_committed_content
                if revision is None or content is None:
                    continue
                if state.descriptor is None:
                    raise RuntimeError(
                        "冻结 source 缺少 owner 重新绑定的 descriptor: " + state.source_id
                    )
                sources.append(ContextSourceSnapshot(
                    descriptor=state.descriptor,
                    revision=revision,
                    content=content,
                    applied_revision=state.latest_visible_committed_revision,
                    tracked=state.tracked,
                ))
            return PendingContextSourceBatch(
                deltas=tuple(self._pending.values()), sources=tuple(sources),
            )

        def prepare_pending(self) -> PendingContextSourceBatch | None:
            rebuild = self.refresh_context_rebuild_state()
            if rebuild is not None:
                return self.prepare_rebuild_sources(rebuild)
            if not self._pending:
                return None
            return self.prepare_sources(rebuild)

        def acknowledge_sealed_pending(
            self,
            batch: PendingContextSourceBatch,
            binding: ContextSealBinding,
        ) -> CommittedContextSourceBatch:
            """只确认真实 seal receipt；来源 applied 的唯一 writer 是 assembly 事务。

            receipt 对应冻结 revision 和完整正文，晚到 desired 保持 pending。
            确认不写控制状态，不消费 source intent，不清空其它批次。
            """
            if not isinstance(binding, ContextSealBinding):
                raise TypeError("CSM 确认需要 ContextSealBinding")
            last = self._last_model_call_receipt
            if last is not None and (
                last.assembly_id == binding.prefix.assembly_id
                and last.deltas == batch.deltas
            ):
                return last
            applied = {source.source_id: source for source in binding.source_revisions}
            frozen = {source.descriptor.source_id: source for source in batch.sources}
            for delta in batch.deltas:
                sealed = applied.get(delta.source_id)
                state = self._sources.get(delta.source_id)
                if state is None:
                    raise KeyError("Context source 不存在: " + delta.source_id)
                if sealed is None or (
                    sealed.revision != delta.revision
                    or sealed.previous_revision != delta.previous_revision
                    or state.latest_visible_committed_revision != delta.previous_revision
                ):
                    raise RuntimeError(
                        "context-source-seal-receipt-conflict: receipt 与冻结 source 不一致: "
                        + delta.source_id
                    )
            port, owner = self._control_state_port, self._owner
            if port is not None and owner is not None:
                persisted = {
                    state.source_id: state
                    for state in port.load_context_source_control_states(owner)
                }
                for delta in batch.deltas:
                    stored = persisted.get(delta.source_id)
                    if stored is None or stored.owner != owner or (
                        stored.latest_visible_committed_revision != delta.revision
                    ):
                        raise RuntimeError(
                            "context-source-seal-receipt-conflict: durable owner 尚未 applied: "
                            + delta.source_id
                        )
                self._sync_persisted_control_states(set(applied) & set(frozen))
            for delta in batch.deltas:
                state = self._sources[delta.source_id]
                state.latest_visible_committed_revision = delta.revision
                state.latest_visible_committed_content = frozen[delta.source_id].content
                self._pending.pop(delta.source_id, None)
                state.observed_revisions = [
                    revision for revision in state.observed_revisions
                    if revision not in delta.observation_provenance
                ]
                if state.latest_revision == delta.revision:
                    state.pending_kind = None
                    state.observed_revisions.clear()
                else:
                    state.pending_kind = "delta"
                    self._queue_delta(state)
            receipt = CommittedContextSourceBatch(
                deltas=batch.deltas, assembly_id=binding.prefix.assembly_id,
            )
            self._last_model_call_receipt = receipt
            for source in batch.sources:
                state = self._sources[source.descriptor.source_id]
                sealed = applied.get(source.descriptor.source_id)
                if sealed is not None:
                    state.latest_visible_committed_revision = sealed.revision
                if state.latest_visible_committed_revision == source.revision:
                    state.latest_visible_committed_content = source.content
            for delta in batch.deltas:
                self._publish_lifecycle_event(
                    source_id=delta.source_id,
                    source_kind=delta.source_kind,
                    kind="committed",
                    revision=delta.revision,
                )
            return receipt

        def _publish_lifecycle_event(
            self,
            *,
            source_id: str,
            source_kind: str,
            kind: str,
            revision: str | None,
        ) -> None:
            """把来源生命周期通知交给注入的发布出口；未注入时是无操作。"""
            sink = self._lifecycle_event_sink
            if sink is None:
                return
            owner = self._owner
            sink(
                ContextSourceEvent(
                    source_id=source_id,
                    source_kind=source_kind,
                    kind=kind,
                    revision=revision,
                    session_id=owner.session_id if owner is not None else None,
                    thread_id=owner.thread_id if owner is not None else None,
                )
            )



__all__ = ["SourceLifecycleMixin"]
