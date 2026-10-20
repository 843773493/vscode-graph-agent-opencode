"""Context source registration 与 durable control state owner。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
        ContextSourceControlMutation,
        ContextSourceControlOperation,
        ContextSourceControlState,
        ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceDescriptor,
        ContextSourceTrackingStateConflict,
        _normalize_skill_name,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
        SourceState,
)


class SourceRegistryMixin:
        def refresh_control_states(self) -> None:
            """初始化及新 activation 边界从唯一 owner 对齐已提交控制状态。

            不读取来源正文；相同 revision 的内存正文和未提交 pending 保留。
            名称索引仍由 effective catalog 决定，重水合不改绑原 tracked 来源。
            """
            owner, port = self._owner, self._control_state_port
            if owner is None or port is None:
                return
            refreshed: dict[str, SourceState] = {}
            seen: set[str] = set()
            for stored in port.load_context_source_control_states(owner):
                if stored.owner != owner:
                    raise RuntimeError(
                        "ContextSourceManager 恢复的控制状态 owner 不匹配: "
                        f"expected=({owner.session_id},{owner.thread_id}) "
                        f"actual=({stored.owner.session_id},{stored.owner.thread_id})"
                    )
                if stored.source_id in seen:
                    raise RuntimeError(
                        "ContextSourceManager 持久化控制状态存在重复 source_id: "
                        f"source_id={stored.source_id}"
                    )
                seen.add(stored.source_id)
                old = self._sources.get(stored.source_id)
                if old is not None and stored.state_revision <= old.persisted_state_revision:
                    if stored.state_revision != old.persisted_state_revision or stored.durable_fields() != old.persisted_fields:
                        raise RuntimeError("context-source-control-state-conflict: 持久控制版本回退或同版本事实变化")
                    continue
                state = SourceState.from_control_state(stored)
                if old is not None:
                    if old.descriptor is not None:
                        state.bind_descriptor(old.descriptor)
                    if old.latest_revision == state.latest_revision:
                        state.latest_content = old.latest_content
                        state.observed_revisions = list(old.observed_revisions)
                    if old.latest_visible_committed_revision == state.latest_visible_committed_revision:
                        state.latest_visible_committed_content = old.latest_visible_committed_content
                    pending = self._pending.get(stored.source_id)
                    if pending is not None and pending.revision == state.latest_revision and state.latest_revision != state.latest_visible_committed_revision:
                        state.pending_kind = old.pending_kind
                refreshed[stored.source_id] = state

            tracked_by_name: dict[str, str] = {}
            for state in (self._sources | refreshed).values():
                if state.tracked:
                    normalized = _normalize_skill_name(state.name)
                    previous = tracked_by_name.get(normalized)
                    if previous is not None:
                        raise ContextSourceTrackingStateConflict(
                            "tracking-state-conflict: 持久化控制状态存在同 normalized "
                            f"name 的多个 active registration: name={normalized} "
                            f"source_ids={sorted((previous, state.source_id))}"
                        )
                    tracked_by_name[normalized] = state.source_id
            for source_id, state in refreshed.items():
                if source_id not in self._sources:
                    self._source_ids_by_name[_normalize_skill_name(state.name)] = source_id
                self._sources[source_id] = state
                if state.pending_kind is None:
                    self._pending.pop(source_id, None)
                if not state.tracked:
                    self._pending_observations.pop(source_id, None)

        def register(
            self,
            descriptor: ContextSourceDescriptor,
            *,
            binding_revision: str | None = None,
            tracking_status: ContextSourceTrackingStatus = "untracked",
        ) -> None:
            """登记来源；恢复后的 registration 在这里重新绑定 Registry descriptor。

            ``tracking_status`` 允许受信来源（如 AGENTS.md）以 tracked 身份登记，
            使其首帧与后续 delta 都能走事件驱动的 pending 路径；已存在的
            registration 重新绑定时保持原 tracking 状态，不因重复注册降级。

            Skill source owner 从冻结 activation snapshot 的 Skill catalog facet
            revision 提供 ``binding_revision``，表示目录/来源绑定版本；它不同于
            observation 产生的来源内容 ``latest_revision``，不得将两者互相冒充。
            """
            if binding_revision is not None and (
                not isinstance(binding_revision, str) or not binding_revision
            ):
                raise ValueError("register.binding_revision 必须是非空字符串或 None")
            if tracking_status not in {"tracked", "untracked"}:
                raise ValueError(
                    "register.tracking_status 只能是 tracked 或 untracked: "
                    f"{tracking_status!r}"
                )
            self.refresh_context_rebuild_state()
            state = self._sources.get(descriptor.source_id)
            if state is None:
                state = SourceState(
                    source_id=descriptor.source_id,
                    source_kind=descriptor.source_kind,
                    name=descriptor.name,
                    descriptor=descriptor,
                    description=descriptor.description,
                    binding_revision=binding_revision,
                    tracking_status=tracking_status,
                )
            else:
                state = replace(state)
                state.bind_descriptor(descriptor)
                if binding_revision is not None:
                    state.binding_revision = binding_revision
            self._persist_control_state(state, operation="register")
            self._sources[descriptor.source_id] = state
            # 名称索引保持最近一次 catalog 注册顺序；同名高优先级 entry 覆盖
            # 索引但不自动改绑既有 active registration（tasks 3.3-A），
            # untrack 仍按 active tracked registration 定位原 registration。
            normalized_name = _normalize_skill_name(descriptor.name)
            self._source_ids_by_name.pop(normalized_name, None)
            self._source_ids_by_name[normalized_name] = descriptor.source_id

        def _set_tracking_status(
            self, state: SourceState, status: ContextSourceTrackingStatus,
        ) -> SourceState:
            """唯一 owner 确认 tracking 后再替换内存，写入失败允许原状态重试。"""
            if status not in {"tracked", "untracked"}:
                raise ValueError(f"非法 tracking_status: {status!r}")
            candidate = replace(state, tracking_status=status)
            self._persist_control_state(
                candidate, operation="track" if status == "tracked" else "untrack",
            )
            self._sources[state.source_id] = candidate
            return candidate

        def metadata(self) -> tuple[Mapping[str, str], ...]:
            """仅返回模型可见的 name/description，不返回路径。"""
            result: list[Mapping[str, str]] = []
            for state in self._registered_states():
                descriptor = state.descriptor
                if descriptor is None:
                    continue
                result.append(
                    {"name": descriptor.name, "description": descriptor.description}
                )
            return tuple(result)

        def descriptors(self) -> tuple[ContextSourceDescriptor, ...]:
            """返回已注册来源的内部 descriptor，供受信 source owner 对账。"""
            return tuple(
                state.descriptor
                for state in self._registered_states()
                if state.descriptor is not None
            )

        def _registered_states(self) -> tuple[SourceState, ...]:
            """按最近一次 register 的 catalog 顺序返回有 descriptor 的状态。"""
            states: list[SourceState] = []
            for source_id in self._source_ids_by_name.values():
                state = self._sources.get(source_id)
                if state is not None and state.descriptor is not None:
                    states.append(state)
            return tuple(states)

        def _persist_control_state(
            self,
            state: SourceState,
            *,
            operation: ContextSourceControlOperation,
        ) -> None:
            """把具有跨重启价值的 registration 状态经唯一 owner 端口写入。

            本端口只保存 observed/tracking；applied 只能由 assembly seal 事务推进。
            """
            port = self._control_state_port
            owner = self._owner
            if port is None or owner is None:
                return
            if not state.has_durable_interest():
                return
            durable = next((
                stored for stored in port.load_context_source_control_states(owner)
                if stored.source_id == state.source_id
            ), None)
            if durable is not None and durable.state_revision != state.persisted_state_revision:
                # seal 可以在 CSM 确认前推进 applied；观察路径只对齐这一个变化，
                # 不吸收其它 writer 的 observed/tracking 漂移。
                before_seal = replace(
                    durable, latest_visible_committed_revision=state.latest_visible_committed_revision,
                )
                # 真实 rebuild seal 同时提交 frozen observed/applied；确认前晚到的
                # observation 只吸收这一精确 receipt 的 CAS，不覆盖其它控制变化。
                batch = self._rebuild_source_batch
                frozen = next((
                    source for source in (() if batch is None else batch.sources)
                    if source.descriptor.source_id == state.source_id
                ), None)
                original = self._rebuild_control_states.get(state.source_id)
                if frozen is not None and original is not None and (
                    frozen.expected_control_revision is not None
                    and durable.state_revision == frozen.expected_control_revision + 1
                    and durable.latest_revision == frozen.revision
                    and durable.latest_visible_committed_revision == frozen.revision
                ):
                    before_seal = replace(
                        before_seal, latest_revision=original.latest_revision,
                    )
                if before_seal.durable_fields() != state.persisted_fields:
                    raise RuntimeError(
                        "context-source-control-state-conflict: observed/tracking 已被其它 writer 推进"
                    )
            snapshot = ContextSourceControlState(
                owner=owner,
                source_id=state.source_id,
                source_kind=state.source_kind,
                name=state.name,
                binding_revision=state.binding_revision,
                tracking_status=state.tracking_status,
                latest_visible_committed_revision=(
                    durable.latest_visible_committed_revision if durable is not None else None
                ),
                latest_revision=state.latest_revision,
                state_revision=durable.state_revision if durable is not None else state.persisted_state_revision,
            )
            if durable is not None and durable.durable_fields() == snapshot.durable_fields():
                state.persisted_state_revision = durable.state_revision
                state.persisted_fields = durable.durable_fields()
                return
            stored = port.apply_context_source_control_mutation(
                ContextSourceControlMutation(operation, snapshot),
            )
            if stored.owner != owner or stored.source_id != state.source_id:
                raise RuntimeError(
                    "context source 控制状态端口返回了不匹配的 registration: "
                    f"expected=({owner.session_id},{owner.thread_id},"
                    f"{state.source_id}) "
                    f"actual=({stored.owner.session_id},{stored.owner.thread_id},"
                    f"{stored.source_id})"
                )
            state.persisted_state_revision = stored.state_revision
            state.persisted_fields = stored.durable_fields()

        def _sync_persisted_control_states(self, source_ids: set[str]) -> None:
            """按 owner durable truth 重新校准内存 CAS 基准。

            model_call 提交经 mutation intent 端口落在唯一 owner 事务里，会推进
            SQLite 的 ``state_revision``，但端口调用不返回确认值。若不在此同步，
            同一 CSM 后续的 observe/untrack/rebind 会以陈旧 ``state_revision``
            触发 CAS 冲突。只同步持久化基准字段，不覆盖内存 revision 真值。
            """
            port = self._control_state_port
            owner = self._owner
            if port is None or owner is None:
                return
            for stored in port.load_context_source_control_states(owner):
                if stored.source_id not in source_ids:
                    continue
                state = self._sources.get(stored.source_id)
                if state is None:
                    continue
                if (
                    stored.owner != owner
                    or stored.source_kind != state.source_kind
                    or stored.name != state.name
                    or stored.binding_revision != state.binding_revision
                    or stored.tracking_status != state.tracking_status
                ):
                    raise RuntimeError(
                        "context-source-control-state-conflict: seal 确认不得吸收其它 writer 的来源绑定或 tracking 变化"
                    )
                state.persisted_state_revision = stored.state_revision
                state.persisted_fields = stored.durable_fields()


__all__ = ["SourceRegistryMixin"]
