"""Context source registration 与 durable control state owner。"""

from __future__ import annotations

from collections.abc import Mapping

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
        ContextSourceControlState,
        ContextSourceControlStatePort,
        ContextSourceOwnerKey,
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
        def _restore_control_states(
            self,
            owner: ContextSourceOwnerKey,
            port: ContextSourceControlStatePort,
        ) -> None:
            """进程重启/agent 重建时从 owner 恢复 tracked 与 untrack 状态。

            只恢复 identity、catalog 绑定 revision 和 revision 事实；pending
            delta 与正文由下一次 observation/reconciliation 重新产生。
            """
            for stored in port.load_context_source_control_states(owner):
                if stored.owner != owner:
                    raise RuntimeError(
                        "ContextSourceManager 恢复的控制状态 owner 不匹配: "
                        f"expected=({owner.session_id},{owner.thread_id}) "
                        f"actual=({stored.owner.session_id},{stored.owner.thread_id})"
                    )
                if stored.source_id in self._sources:
                    raise RuntimeError(
                        "ContextSourceManager 持久化控制状态存在重复 source_id: "
                        f"source_id={stored.source_id}"
                    )
                self._sources[stored.source_id] = SourceState.from_control_state(stored)
                normalized_name = _normalize_skill_name(stored.name)
                if stored.tracking_status == "tracked":
                    duplicates = sorted(
                        other.source_id
                        for other in self._sources.values()
                        if other.source_id != stored.source_id
                        and other.tracked
                        and _normalize_skill_name(other.name) == normalized_name
                    )
                    if duplicates:
                        raise ContextSourceTrackingStateConflict(
                            "tracking-state-conflict: 持久化控制状态存在同 normalized "
                            f"name 的多个 active registration: name={normalized_name} "
                            f"source_ids={duplicates + [stored.source_id]}"
                        )
                # 同名 registration 按恢复（created_at）顺序让最新者持有名称索引；
                # active tracked registration 的定位不依赖该索引（untrack 走扫描）。
                self._source_ids_by_name[normalized_name] = stored.source_id

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

            TODO: catalog/来源绑定 revision 目前没有生产 producer（SkillCatalog
            revision 发布属于 OpenSpec 3.3/4.1-A）。落地后必须由 source owner
            传入 ``binding_revision``，不得用内容 revision 冒充目录绑定 revision。
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
                self._sources[descriptor.source_id] = state
            else:
                state.bind_descriptor(descriptor)
                if binding_revision is not None:
                    state.binding_revision = binding_revision
            # 名称索引保持最近一次 catalog 注册顺序；同名高优先级 entry 覆盖
            # 索引但不自动改绑既有 active registration（tasks 3.3-A），
            # untrack 仍按 active tracked registration 定位原 registration。
            normalized_name = _normalize_skill_name(descriptor.name)
            self._source_ids_by_name.pop(normalized_name, None)
            self._source_ids_by_name[normalized_name] = descriptor.source_id
            self._persist_control_state(state)

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
            applied_revision: str | None = None,
        ) -> None:
            """把具有跨重启价值的 registration 状态经唯一 owner 端口写入。

            applied_revision 显式指定提交时刻的 applied 基准（model_call 提交路径
            传 state.latest_revision）；缺省时使用内存当前 applied 值。
            """
            port = self._control_state_port
            owner = self._owner
            if port is None or owner is None:
                return
            if not state.has_durable_interest():
                return
            snapshot = ContextSourceControlState(
                owner=owner,
                source_id=state.source_id,
                source_kind=state.source_kind,
                name=state.name,
                binding_revision=state.binding_revision,
                tracking_status=state.tracking_status,
                latest_visible_committed_revision=(
                    state.latest_visible_committed_revision
                    if applied_revision is None
                    else applied_revision
                ),
                latest_revision=state.latest_revision,
                state_revision=state.persisted_state_revision,
            )
            if state.persisted_fields == snapshot.durable_fields():
                return
            stored = port.save_context_source_control_state(snapshot)
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


__all__ = ["SourceRegistryMixin"]
