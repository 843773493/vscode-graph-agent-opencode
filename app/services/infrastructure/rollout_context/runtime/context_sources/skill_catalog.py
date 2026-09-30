"""Skill catalog activation 与 load owner。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.runtime.context_sources.delta import (
        _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceDescriptor,
        ContextSourceTrackingStateConflict,
        SkillCatalogActivationSnapshot,
        SkillCatalogBinding,
        SkillCatalogSnapshotConflict,
        SkillLoadAppendStatus,
        SkillLoadMode,
        SkillLoadReceipt,
        SkillLoadStatus,
        _normalize_skill_name,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
        SourceState,
)


class SkillCatalogMixin:
        def install_skill_activation_snapshot(
            self,
            snapshot: SkillCatalogActivationSnapshot,
        ) -> None:
            """在 activation boundary 安装冻结的 SkillCatalog activation snapshot。

            只能由 source owner（Skill middleware/factory 装配）在边界调用。
            本方法只冻结当前 catalog 事实，不做 registration 对账；binding 与
            live 来源的不一致由 load 解析路径 fail closed，同名高优先级覆盖
            （rebind 前夕）允许安装。
            """
            if not isinstance(snapshot, SkillCatalogActivationSnapshot):
                raise TypeError(
                    "install_skill_activation_snapshot 需要 SkillCatalogActivationSnapshot"
                )
            self._skill_activation_snapshot = snapshot

        def _resolve_activation_binding(self, name: str) -> SkillCatalogBinding:
            """从冻结 snapshot 解析 exact binding；缺失/不一致 fail closed。"""
            snapshot = self._skill_activation_snapshot
            if snapshot is None:
                raise SkillCatalogSnapshotConflict(
                    "skill-catalog-snapshot-conflict: 尚无冻结 SkillCatalog "
                    "activation snapshot；snapshot/tracked 只能从冻结 snapshot 解析"
                )
            binding = snapshot.entries.get(name)
            if binding is None:
                raise KeyError(f"Skill 不存在: name={name}")
            # 按当前 effective entry 解析已登记 registration（与 load_skill 同一
            # 名称索引），不假设固定 ``skill:{name}`` source identity：同名高优先级
            # entry 覆盖后，对应 registration 的 display_uri 才是该 binding 的对照。
            registered_source_id = self._source_ids_by_name.get(binding.name)
            state = (
                self._sources.get(registered_source_id)
                if registered_source_id is not None
                else None
            )
            if (
                state is not None
                and state.descriptor is not None
                and state.descriptor.resource_uri is not None
                and state.descriptor.resource_uri != binding.display_uri
            ):
                raise SkillCatalogSnapshotConflict(
                    "skill-catalog-snapshot-conflict: 冻结 binding 与已登记来源不一致: "
                    f"name={name} frozen={binding.display_uri} "
                    f"registered={state.descriptor.resource_uri}"
                )
            return binding

        def load_skill(self, name: str, mode: SkillLoadMode = "snapshot") -> SkillLoadReceipt:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("skill_load.name 必须是非空字符串")
            if mode not in {"snapshot", "tracked", "untrack"}:
                raise ValueError("skill_load.mode 只能是 snapshot、tracked 或 untrack")
            normalized = _normalize_skill_name(name)
            source_id = self._source_ids_by_name.get(normalized)
            if source_id is None:
                raise KeyError(f"Skill 不存在: name={normalized}")
            state = self._sources[source_id]
            display_uri = (
                state.descriptor.resource_uri if state.descriptor is not None else None
            )
            if mode == "untrack":
                # untrack 按唯一 active tracked registration 定位，不按 catalog
                # 优先级重新解析 entry（design 5.1）：同名高优先级 entry 出现后
                # 仍能停止原 tracking，也不读取 source。
                tracked_states = [
                    candidate
                    for candidate in self._sources.values()
                    if candidate.tracked
                    and _normalize_skill_name(candidate.name) == normalized
                ]
                if len(tracked_states) > 1:
                    raise ContextSourceTrackingStateConflict(
                        "tracking-state-conflict: 同 normalized name 存在多个 "
                        f"active registration: name={normalized} "
                        f"source_ids={sorted(item.source_id for item in tracked_states)}"
                    )
                target = tracked_states[0] if tracked_states else None
                if target is not None:
                    self._set_tracking_status(target, "untracked")
                    # untrack(frozen) 状态必须跨重启保留：重启后不得恢复跟踪或重新注入。
                    self._persist_control_state(target)
                    # OpenSpec 3.8-C：untrack 成功边界发布轻量事件（内存通知）。
                    self._publish_lifecycle_event(
                        source_id=target.source_id,
                        source_kind=target.source_kind,
                        kind="untracked",
                        revision=target.latest_revision,
                    )
                    display_uri = (
                        target.descriptor.resource_uri
                        if target.descriptor is not None
                        else None
                    )
                # 无 active registration 时返回确定性 not_tracked：不改状态、
                # 不发布事件、不伪造成功；revision 事实仍取定位到的 registration
                # （未命中时为当前 catalog entry）。
                resolved = target if target is not None else state
                return SkillLoadReceipt(
                    name=name,
                    mode=mode,
                    revision=resolved.latest_revision,
                    queued=False,
                    tracked=False,
                    display_uri=display_uri,
                    content_hash=(
                        _revision(resolved.latest_content)
                        if resolved.latest_content is not None
                        else None
                    ),
                    status="loaded" if target is not None else "not_tracked",
                    append_status="none",
                )

            # snapshot/tracked 只从冻结 Turn/ModelCall SkillCatalogSnapshot 的
            # exact binding 解析；同名 catalog 新 revision 不影响已冻结调用。
            binding = self._resolve_activation_binding(normalized)
            source_rebound = False
            if state.latest_revision is None:
                self.activate_skill_content(
                    normalized,
                    binding.body,
                    revision=binding.activation_revision,
                )
            elif (
                state.latest_revision != binding.activation_revision
            ):
                raise SkillCatalogSnapshotConflict(
                    "skill-catalog-snapshot-conflict: 冻结 binding 与 live activation "
                    f"状态不一致: name={normalized} frozen={binding.activation_revision} "
                    f"live={state.latest_revision}"
                )
            if mode == "tracked":
                conflicting = [
                    candidate
                    for candidate in self._sources.values()
                    if candidate.source_id != source_id
                    and candidate.tracked
                    and _normalize_skill_name(candidate.name) == normalized
                ]
                if conflicting:
                    # 判据是 resolved source identity（registration 绑定的
                    # ``name + catalog_entry_identity + resolved_source_identity``
                    # 的 model-visible 投影），不是时间戳或物理路径。
                    rebound = self._rebound_registrations(
                        conflicting, normalized, binding.display_uri
                    )
                    if rebound is not None:
                        # effective entry identity 已变：在同一 CSM owner mutation
                        # 内冻结旧 registration，并为当前 effective entry 建新 tracking。
                        self._freeze_rebound_registration(rebound)
                        source_rebound = True
                self._set_tracking_status(state, "tracked")
                self._persist_control_state(state)
            status: SkillLoadStatus
            append_status: SkillLoadAppendStatus
            if source_id in self._pending:
                # rebind 成功态必须独立于 loaded，不得复用 loaded 冒充。
                status = "rebound" if source_rebound else "loaded"
                append_status = "appended"
            elif state.latest_visible_committed_revision == state.latest_revision:
                # 同 source/revision 已在 active view 可见：不追加第二个 item。
                status = "already_active"
                append_status = "already_active"
            else:
                raise RuntimeError(
                    "Skill revision 状态不一致：既无待提交 item 也未 applied；"
                    f"name={normalized} latest={state.latest_revision} "
                    f"applied={state.latest_visible_committed_revision}"
                )
            return SkillLoadReceipt(
                name=normalized,
                mode=mode,
                revision=binding.activation_revision,
                queued=source_id in self._pending,
                tracked=state.tracked,
                display_uri=binding.display_uri,
                content_hash=binding.content_hash,
                status=status,
                append_status=append_status,
                source_rebound=source_rebound,
            )

        def _rebound_registrations(
            self,
            conflicting: list[SourceState],
            normalized: str,
            effective_identity: str | None,
        ) -> SourceState | None:
            """机械判定显式 tracked 是否应触发 rebind。

            判据只有 resolved source identity（registration 的 display/resource
            URI），不使用时间戳或物理路径。返回需要冻结的旧 registration；
            effective entry 未变（同名同 identity 的重复 active registration）时
            按 3.14 合同 fail closed，绝不静默改绑或重复建 tracking。
            """
            if len(conflicting) > 1:
                raise ContextSourceTrackingStateConflict(
                    "tracking-state-conflict: 同 normalized name 存在多个 "
                    f"active registration: name={normalized} "
                    f"source_ids={sorted(item.source_id for item in conflicting)}"
                )
            existing = conflicting[0]
            descriptor = existing.descriptor
            existing_identity = (
                descriptor.resource_uri if descriptor is not None else None
            )
            if existing_identity is None:
                raise ContextSourceTrackingStateConflict(
                    "tracking-state-conflict: 同名旧 registration 尚未绑定当前 "
                    "catalog descriptor，无法机械判定 effective entry 是否已变，"
                    f"拒绝静默改绑: name={normalized} source_id={existing.source_id}"
                )
            if existing_identity == effective_identity:
                raise ContextSourceTrackingStateConflict(
                    "tracking-state-conflict: 同名同 effective entry 存在重复 "
                    f"active registration: name={normalized} "
                    f"source_ids={[existing.source_id, self._source_ids_by_name.get(normalized)]}"
                )
            return existing

        def _freeze_rebound_registration(self, state: SourceState) -> None:
            """显式 rebind 时把旧 registration 冻结为 untracked 并持久化。

            复用 3.5 的唯一 tracking 原语：只改 tracking 状态、不删除、不重写
            已提交 item；旧 context item 逐字节保持不变。按当前 effective entry
            的 source identity 唯一裁决，不做 name-only 猜测。
            """
            self._set_tracking_status(state, "untracked")
            self._persist_control_state(state)
            # OpenSpec 3.8-C：tracking 状态变化的成功边界发布轻量事件（内存通知）。
            self._publish_lifecycle_event(
                source_id=state.source_id,
                source_kind=state.source_kind,
                kind="untracked",
                revision=state.latest_revision,
            )

        def descriptor_for_skill(self, name: str) -> ContextSourceDescriptor:
            """只给受信 source owner 解析内部 descriptor，不进入模型协议。"""
            source_id = self._source_ids_by_name.get(name)
            if source_id is None:
                raise KeyError(f"Skill 不存在: name={name}")
            descriptor = self._sources[source_id].descriptor
            if descriptor is None:
                raise RuntimeError(
                    "Skill 来源尚未由 source owner 重新注册，不能解析内部 locator: "
                    f"name={name}"
                )
            return descriptor

        def activate_skill_content(
            self,
            name: str,
            content: str,
            *,
            revision: str | None = None,
        ) -> None:
            """接收 source owner 已验证的正文；CSM 不执行 I/O。"""
            descriptor = self.descriptor_for_skill(name)
            state = self._sources[descriptor.source_id]
            if not isinstance(content, str):
                raise TypeError(
                    "ContextSourceManager.activate_skill_content.content 必须是字符串"
                )
            effective_revision = revision or _revision(content)
            if state.latest_revision is not None:
                if (
                    state.latest_revision == effective_revision
                    and state.latest_content == content
                ):
                    return
                if state.latest_visible_committed_revision is not None and not state.tracked:
                    raise RuntimeError(
                        "snapshot Skill 已激活，变化必须先通过 tracked source observation 提交"
                    )
            self._record_observation(state, effective_revision)
            state.latest_revision = effective_revision
            state.latest_content = content
            state.pending_kind = "activation" if state.latest_visible_committed_revision is None else "delta"
            self._queue_delta(state)
            self._persist_control_state(state)



__all__ = ["SkillCatalogMixin"]
