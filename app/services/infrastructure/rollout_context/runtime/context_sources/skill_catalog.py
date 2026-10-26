"""Skill catalog activation 与 load owner。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.delta import (
    _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceTrackingStateConflict,
    PendingSourceObservation,
    PublishedContextSourceRevision,
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


def skill_catalog_from_activation(
    activation: ResourceActivationSnapshotRef,
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
) -> SkillCatalogActivationSnapshot | None:
    """从同一冻结 activation 的 catalog/metadata/正文恢复 exact Skill binding。"""
    by_id = {binding.resource_id: binding for binding in activation.bindings if binding.resource_kind == "skill"}
    catalogs = tuple(binding for binding in by_id.values() if binding.facet == "catalog")
    if not catalogs:
        if by_id:
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill facets 缺少冻结 catalog")
        return None
    if len(catalogs) != 1:
        raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill catalog 不唯一")
    payload = read_payload(activation, catalogs[0])
    if not isinstance(payload, Mapping) or set(payload) != {"entries"} or not isinstance(payload["entries"], list):
        raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill catalog 正文非法")
    sources = {}
    activation_by_uri = {}
    for ref in by_id.values():
        if ref.facet == "activation":
            if ref.display_uri in activation_by_uri:
                raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: 同来源 activation 重复")
            activation_by_uri[ref.display_uri] = ref
    for metadata_ref in by_id.values():
        if metadata_ref.facet != "metadata":
            continue
        metadata = read_payload(activation, metadata_ref)
        uri = metadata_ref.display_uri
        metadata_suffix = ":metadata"
        if not metadata_ref.resource_id.endswith(metadata_suffix):
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill metadata descriptor identity 非法")
        entry_identity = metadata_ref.resource_id[:-len(metadata_suffix)]
        activation_ref = activation_by_uri.get(uri)
        if uri is None or activation_ref is None or uri in sources or not entry_identity or (
            activation_ref.resource_id != f"{entry_identity}:activation"
        ) or not isinstance(metadata, Mapping) or set(metadata) != {
            "name", "description",
        } or not isinstance(metadata["name"], str) or not metadata["name"] or not isinstance(metadata["description"], str):
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill metadata 正文非法")
        body = read_payload(activation, activation_ref)
        if not isinstance(body, Mapping) or set(body) != {"body"} or not isinstance(body["body"], str):
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill activation 正文非法")
        sources[uri] = SkillCatalogBinding(
            name=metadata["name"], resource_id=activation_ref.resource_id,
            entry_identity=entry_identity, display_uri=uri,
            activation_revision=activation_ref.revision, body=body["body"],
            description=metadata["description"],
        )
    if set(sources) != set(activation_by_uri):
        raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill activation 缺少 metadata")
    entries = {}
    for entry in payload["entries"]:
        if not isinstance(entry, Mapping) or set(entry) != {
            "name", "layer", "entry_identity", "display_uri", "metadata_resource_id",
            "activation_resource_id", "metadata_revision", "activation_revision",
        }:
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill catalog entry 非法")
        metadata_ref = by_id.get(entry["metadata_resource_id"])
        activation_ref = by_id.get(entry["activation_resource_id"])
        if metadata_ref is None or activation_ref is None or (
            metadata_ref.facet != "metadata" or activation_ref.facet != "activation"
            or metadata_ref.revision != entry["metadata_revision"]
            or activation_ref.revision != entry["activation_revision"]
            or metadata_ref.display_uri != entry["display_uri"]
            or activation_ref.display_uri != entry["display_uri"]
        ):
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: catalog 与冻结 Skill facets 不一致")
        binding = sources.get(entry["display_uri"])
        if binding is None or binding.name != entry["name"] or binding.entry_identity != entry["entry_identity"]:
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: catalog 与来源 identity 不一致")
        name = entry["name"]
        if name in entries:
            raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: Skill name 重复")
        entries[name] = binding
    return SkillCatalogActivationSnapshot(catalog_revision=catalogs[0].revision, entries=entries, sources=sources)


class SkillCatalogMixin:
        _sources: dict[str, SourceState]
        _source_ids_by_kind_and_name: dict[tuple[str, str], str]
        _queue_delta: Callable[[SourceState], None]
        _pending: dict[str, ContextSourceDelta]
        _pending_observations: dict[str, PendingSourceObservation]
        _published_source_revisions: dict[str, PublishedContextSourceRevision]

        def frozen_skill_catalog(self) -> SkillCatalogActivationSnapshot:
            if self._skill_activation_snapshot is None:
                raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: 尚无冻结 Skill catalog")
            return self._skill_activation_snapshot

        def register_skill_binding(self, binding: SkillCatalogBinding, *, catalog_revision: str) -> None:
            """entry identity 决定 registration；同名新 entry 保留原 tracked registration。"""
            self.register(ContextSourceDescriptor(
                source_id=f"skill:{binding.entry_identity}", source_kind="skill",
                name=binding.name, description=binding.description, internal_locator=binding.display_uri,
                resource_uri=binding.display_uri,
            ), binding_revision=catalog_revision)

        def install_skill_activation_snapshot(
            self,
            snapshot: SkillCatalogActivationSnapshot,
        ) -> None:
            """在 activation boundary 安装冻结的 SkillCatalog activation snapshot。

            只能由 source owner（Skill middleware/factory 装配）在边界调用。
            本方法只冻结当前 catalog 事实，不做 registration 对账；load 采用
            调用冻结的 exact binding，同名高优先级覆盖不会自动改绑追踪。
            """
            if not isinstance(snapshot, SkillCatalogActivationSnapshot):
                raise TypeError(
                    "install_skill_activation_snapshot 需要 SkillCatalogActivationSnapshot"
                )
            self._skill_activation_snapshot = snapshot

        def observe_frozen_tracked_skills(self) -> None:
            """按原来源 identity 对账 tracked；同名覆盖不改绑，删除明确拒绝。"""
            snapshot = self.frozen_skill_catalog()
            observations = []
            for state in self._sources.values():
                if state.source_kind != "skill" or not state.tracked:
                    continue
                descriptor = state.descriptor
                binding = snapshot.sources.get(descriptor.resource_uri) if descriptor is not None and descriptor.resource_uri is not None else None
                if binding is None or state.source_id != f"skill:{binding.entry_identity}":
                    raise RuntimeError("tracked-source-unavailable: 冻结 Skill 来源不存在: " + state.source_id)
                observations.append((state.source_id, binding))
            for source_id, binding in observations:
                self.observe(source_id, binding.body, revision=binding.activation_revision).require_tracked()

        def load_skill(self, name: str, mode: SkillLoadMode = "snapshot", *, activation_snapshot: SkillCatalogActivationSnapshot | None = None) -> SkillLoadReceipt:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("skill_load.name 必须是非空字符串")
            if mode not in {"snapshot", "tracked", "untrack"}:
                raise ValueError("skill_load.mode 只能是 snapshot、tracked 或 untrack")
            normalized = _normalize_skill_name(name)
            binding = None
            if mode != "untrack" and activation_snapshot is not None:
                binding = activation_snapshot.entries.get(normalized)
                if binding is None:
                    raise KeyError(f"Skill 不存在: name={normalized}")
                state = self._registration_candidate(ContextSourceDescriptor(
                    source_id=f"skill:{binding.entry_identity}", source_kind="skill",
                    name=binding.name, description=binding.description,
                    internal_locator=binding.display_uri, resource_uri=binding.display_uri,
                ), activation_snapshot.catalog_revision)
                source_id = state.source_id
            else:
                source_id = self._source_ids_by_kind_and_name.get(("skill", normalized))
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
                    if candidate.source_kind == "skill" and candidate.tracked
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
                    # untrack(frozen) 状态必须跨重启保留：重启后不得恢复跟踪或重新注入。
                    target = self._set_tracking_status(target, "untracked")
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
            if binding is None:
                snapshot = self.frozen_skill_catalog()
                binding = snapshot.entries.get(normalized)
                if binding is None:
                    raise KeyError(f"Skill 不存在: name={normalized}")
            if state.descriptor is not None and state.descriptor.resource_uri is not None and (
                state.descriptor.resource_uri != binding.display_uri
            ):
                raise SkillCatalogSnapshotConflict(
                    "skill-catalog-snapshot-conflict: 冻结 binding 与已登记来源不一致: "
                    f"name={normalized} frozen={binding.display_uri} registered={state.descriptor.resource_uri}"
                )
            conflicting = [
                candidate for candidate in self._sources.values()
                if candidate.source_kind == "skill" and candidate.source_id != source_id and candidate.tracked
                and _normalize_skill_name(candidate.name) == normalized
            ]
            rebound = self._rebound_registrations(conflicting, normalized, binding.display_uri) if conflicting else None
            state = self._accept_skill_activation(state, rebound, binding, mode)
            source_rebound = rebound is not None
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
            """机械判定本次显式加载是否应冻结同名旧 tracking。

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
                    f"source_ids={[existing.source_id, self._source_ids_by_kind_and_name.get(('skill', normalized))]}"
                )
            return existing

        def _accept_skill_activation(
            self, state: SourceState, old: SourceState | None,
            binding: SkillCatalogBinding, mode: SkillLoadMode,
        ) -> SourceState:
            """同一 owner 接受冻结正文与互斥模式；持久确认后才安装候选及清理旧更新。"""
            for revision, content in (
                (state.latest_revision, state.latest_content),
                (state.latest_visible_committed_revision, state.latest_visible_committed_content),
            ):
                if revision == binding.activation_revision and content is not None and content != binding.body:
                    raise SkillCatalogSnapshotConflict(
                        "skill-catalog-snapshot-conflict: 同一 Skill revision 的正文不一致"
                    )
            candidate = self._source_observation_candidate(state, binding.body, binding.activation_revision)
            # 显式加载采用完整冻结正文；后续自动观察才相对已封存基准生成 diff。
            candidate.pending_kind = "activation"
            candidate.tracking_status = "tracked" if mode == "tracked" else "untracked"
            frozen = replace(old, tracking_status="untracked") if old is not None else None
            self._persist_control_state(candidate, operation="activate_skill", replaced_state=frozen)
            already_visible = (
                candidate.latest_visible_committed_revision == binding.activation_revision
                and candidate.selection_sequence == candidate.latest_visible_selection_sequence
            )
            if already_visible:
                candidate.latest_visible_committed_content = binding.body
                candidate.pending_kind = None
                candidate.observed_revisions.clear()
            if frozen is not None:
                frozen.pending_kind = None
                frozen.observed_revisions = []
                self._sources[frozen.source_id] = frozen
                self._pending.pop(frozen.source_id, None)
                self._pending_observations.pop(frozen.source_id, None)
            # 后一次显式选择取代同名尚未封存的候选，不能在 seal 时重新激活旧来源。
            for other in self._sources.values():
                if other.source_id != candidate.source_id and other.source_kind == "skill" and (
                    _normalize_skill_name(other.name) == _normalize_skill_name(candidate.name)
                ):
                    other.pending_kind = None
                    self._pending.pop(other.source_id, None)
            self._install_registration(candidate)
            self._pending_observations.pop(candidate.source_id, None)
            if already_visible:
                self._pending.pop(candidate.source_id, None)
            else:
                self._queue_delta(candidate)
            self._published_source_revisions[candidate.source_id] = PublishedContextSourceRevision(
                candidate.source_id, binding.activation_revision, binding.body,
            )
            self._rebuild_source_batch = None
            stopped = frozen if frozen is not None else state if state.tracked and not candidate.tracked else None
            if stopped is not None:
                self._publish_lifecycle_event(
                    source_id=stopped.source_id, source_kind=stopped.source_kind,
                    kind="untracked", revision=stopped.latest_revision,
                )
            return candidate

        def restore_frozen_skill_selections(self) -> None:
            """恢复已接受但尚未封存的选择，只消费同一 execution 冻结的正文版本。"""
            snapshot = self.frozen_skill_catalog()
            for state in self._sources.values():
                if state.source_kind != "skill" or state.selection_sequence <= state.latest_visible_selection_sequence:
                    continue
                if any(other.source_kind == "skill" and _normalize_skill_name(other.name) == _normalize_skill_name(state.name)
                       and other.selection_sequence > state.selection_sequence for other in self._sources.values()):
                    continue
                descriptor = state.descriptor
                binding = snapshot.sources.get(descriptor.resource_uri) if descriptor is not None and descriptor.resource_uri is not None else None
                if binding is None or binding.activation_revision != state.latest_revision:
                    raise SkillCatalogSnapshotConflict("skill-selection-unavailable: 待封存选择缺少精确冻结正文")
                state.latest_content = binding.body
                state.pending_kind = "activation"
                self._queue_delta(state)




__all__ = ["SkillCatalogMixin", "skill_catalog_from_activation"]
