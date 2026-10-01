"""Context source observation、pending 与 durable commit 生命周期。"""

from __future__ import annotations

from dataclasses import replace

from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
        ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.delta import (
        _adopt_restored_baseline,
        _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        CommittedContextSourceBatch,
        PendingContextSourceBatch,
        PendingSourceObservation,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
        SourceObservation,
        build_source_lifecycle_decision,
)


class SourceLifecycleMixin:
        def observe(self, source_id: str, content: str, *, revision: str | None = None) -> bool:
            """接收已登记来源的新快照；未跟踪来源不会改变上下文。"""
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if not isinstance(content, str):
                raise TypeError("ContextSourceManager.observe.content 必须是字符串")
            if not state.tracked:
                return False
            effective_revision = revision or _revision(content)
            if state.latest_revision == effective_revision and state.latest_content == content:
                return False
            if _adopt_restored_baseline(state, content, effective_revision):
                # 重启恢复：该 revision 已在已提交上下文中，只重建 diff 基准。
                return False
            if state.latest_visible_committed_revision is None:
                self._record_observation(state, effective_revision)
                self._apply_content(state, content, kind="activation")
                self._persist_control_state(state)
                return True
            self._record_observation(state, effective_revision)
            state.latest_revision = effective_revision
            state.latest_content = content
            state.pending_kind = "delta"
            self._queue_delta(state)
            self._persist_control_state(state)
            return True

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

        def restore_active_revision(self, source_id: str, active_revision: str | None) -> bool:
            """rewind 后发现最新注入不在 active view 时重排当前有效正文。

            TODO: OpenSpec 3.6 要求 rewind/compaction 按目标 checkpoint 恢复
            tracking state（snapshot/untracked 不恢复）。当前 checkpoint 恢复路径
            还没有调用本方法，也没有 checkpoint-versioned 控制状态回放；在接入
            之前，rewind 只能依赖调用方显式传入 active revision。
            """
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if not state.tracked or state.latest_revision is None:
                return False
            if active_revision == state.latest_revision:
                return False
            state.pending_kind = "rebuild"
            self._queue_delta(state)
            return True

        def prepare_pending(self) -> PendingContextSourceBatch | None:
            pending = tuple(self._pending.values())
            if not pending:
                return None
            return PendingContextSourceBatch(deltas=pending)

        def commit_model_call_pending(
            self,
            batch: PendingContextSourceBatch,
        ) -> CommittedContextSourceBatch:
            """原子提交 model_call 边界的 pending 批次（复用唯一 durable 端口）。

            - 重复提交同一批次复用同一次提交结果：不重复持久化、不重复推进
              applied、不重复发布事件。
            - 批次与当前 pending 不一致（提交前出现新变化）时 fail closed，
              不推进任何 applied revision。
            - 每条 delta 与内存 registration 的身份/revision/diff 基准校验；
              任何漂移都让整批失败，不产生半提交。
            - 控制状态先经唯一 durable 端口逐条持久化（每条一个 owner 事务，CAS
              防并发推进）；全部成功后才推进内存 applied、清空 pending 并发布
              事件。持久化失败时内存零变化，批次保持可重试。
            - 事件只在 durable truth 成功后发布；纯内存 CSM（无 owner）在内存
              真值推进后发布。

            有 Saver owner 时，delta 会先映射为
            ``ApplySourceLifecycleDecision``，再由 owner 在 source item/control state
            的同一事务中提交；无 owner 的纯内存测试仍只验证 CSM 控制状态顺序。
            """
            last = self._last_model_call_receipt
            if last is not None and last.deltas == batch.deltas:
                return last
            if tuple(self._pending.values()) != batch.deltas:
                raise RuntimeError(
                    "Context source pending 在提交前发生变化，拒绝推进 applied revision"
                )
            for delta in batch.deltas:
                state = self._sources.get(delta.source_id)
                if state is None:
                    raise KeyError(f"Context source 不存在: source_id={delta.source_id}")
                if (
                    delta.source_kind != state.source_kind
                    or delta.source_name != state.name
                    or delta.revision != state.latest_revision
                    or delta.previous_revision != state.latest_visible_committed_revision
                ):
                    raise RuntimeError(
                        "Context source pending 与 registration 状态不一致"
                        f"（fence drift），拒绝提交: source_id={delta.source_id}"
                    )
            # 先经唯一 owner intent 端口提交全部 source item/control state；任何
            # 失败都让内存 applied/diff 基准保持不变。纯内存/旧测试装配没有
            # mutation_intent_port 时仍使用注入的控制状态替身，不触碰生产 Saver。
            mutation_consume = (
                getattr(self._mutation_intent_port, "consume_mutation_intent", None)
                if self._mutation_intent_port is not None
                else None
            )
            if mutation_consume is not None:
                if self._owner is None:
                    raise RuntimeError(
                        "ContextSourceManager source intent 提交缺少 owner"
                    )
                decisions = []
                for delta in batch.deltas:
                    state = self._sources[delta.source_id]
                    # 内存 delta 保留 activation 作为首次注入的外部合同；
                    # mutation intent 的 source lifecycle 语义将首次激活表达为 base。
                    decision_kind = "base" if delta.kind == "activation" else delta.kind
                    observation = SourceObservation(
                        owner=self._owner,
                        source_id=delta.source_id,
                        source_kind=delta.source_kind,
                        name=delta.source_name,
                        revision=delta.revision,
                        tracking_mode=("tracked" if state.tracked else "untrack"),
                        from_revision=(
                            delta.previous_revision
                            if decision_kind == "delta"
                            else None
                        ),
                        content=delta.content,
                    )
                    decision = build_source_lifecycle_decision(
                        observation,
                        decision_kind=decision_kind,
                    )
                    decisions.append(
                        replace(
                            decision,
                            item_id=(
                                f"item-context-source:{delta.source_id}:"
                                f"{delta.revision}:{delta.kind}"
                            ),
                        )
                    )
                for decision in decisions:
                    mutation_consume(decision)
            else:
                # 已持久化的 source 在重试时按 durable fields 幂等跳过。
                for delta in batch.deltas:
                    state = self._sources[delta.source_id]
                    self._persist_control_state(
                        state,
                        applied_revision=state.latest_revision,
                    )
            # durable truth 成功后才推进内存真值并清空 pending。
            for delta in batch.deltas:
                state = self._sources[delta.source_id]
                state.latest_visible_committed_revision = state.latest_revision
                state.latest_visible_committed_content = state.latest_content
                state.pending_kind = None
                # provenance 随本次提交闭合：已合并的 observed revisions 不再保留。
                state.observed_revisions.clear()
            self._pending.clear()
            receipt = CommittedContextSourceBatch(deltas=batch.deltas)
            self._last_model_call_receipt = receipt
            # OpenSpec 3.8-C：commit 成功边界发布轻量事件（每个来源一条），
            # 发布发生在 durable truth 成功之后，失败路径不发布。
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
