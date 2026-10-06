"""从正向来源和 canonical facts 编译模型调用前缀；不读取最终请求。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    PendingPrefixEpochTransition,
    ProviderProfileItemFrame,
    append_items,
    initial_epoch_state,
    open_rebuild_epoch,
)
from app.domain.itemized.epoch.root_compilation import (
    RootCandidateSource,
    compile_root_item,
)
from app.domain.itemized.epoch.seal_binding import (
    AppliedSourceRevision,
    ContextSealPreparation,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import (
    contribution_content_hash,
    payload_content_length,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    SourceBinding,
    resolve_contribution_for_ref,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceSnapshot,
    PendingContextSourceBatch,
)
from app.services.mapping.itemized.prefix_frames import project_canonical_prefix_frames


def full_source_id(source: ContextSourceSnapshot) -> str:
    return f"source-full:{source.descriptor.source_id}:{source.revision}"


def source_contributions(batch: PendingContextSourceBatch) -> tuple[ContextContribution, ...]:
    """先登记完整来源，registry 分配唯一 source ordinal；delta 使用独立不可变身份。"""
    contributions = [
        ContextContribution(
            contribution_id=full_source_id(source),
            source_kind=source.descriptor.source_kind,
            source_revision=source.revision,
            body=source.content,
            content_hash=contribution_content_hash("prompt", source.content),
            content_length=payload_content_length("structured_content", source.content),
            source_binding=SourceBinding(source_ref=source.descriptor.source_id),
            root_placement=source.descriptor.root_placement,
        )
        for source in batch.sources
    ]
    contributions.extend(
        ContextContribution(
            contribution_id=f"source-update:{delta.source_id}:{delta.revision}:{delta.kind}",
            source_kind=delta.source_kind,
            source_revision=delta.revision,
            contribution_kind="notice",
            body=delta.content,
            content_hash=contribution_content_hash("notice", delta.content),
            content_length=payload_content_length("structured_content", delta.content),
            source_binding=SourceBinding(source_ref=delta.source_id),
        )
        for delta in batch.deltas
    )
    return tuple(contributions)


def compile_prefix_context(
    plan: ContextRequestPlan,
    items: Sequence[CanonicalItemRecord],
    batch: PendingContextSourceBatch,
    tool_set: ToolSetSnapshot,
    *,
    overlay_bodies: Mapping[str, object],
    previous: ContextAssemblySnapshot | None,
    provider_profile: str,
    turn_id: str | None,
    transition: PendingPrefixEpochTransition | None = None,
    retained_source_assembly: ContextAssemblySnapshot | None = None,
    retained_source_refs: frozenset[tuple[str, str]] = frozenset(),
    retained_source_bodies: Mapping[str, object] | None = None,
) -> tuple[ContextRequestPlan, ContextSealPreparation]:
    """同 epoch 精确继承已封存 prefix，只在真实边界重编 root。"""
    tool_ref = plan.tool_set_refs[0]
    compatibility_key = tool_set.compatibility_key(tool_ref)
    previous_binding = previous.seal_binding if previous is not None else None
    if previous is not None and previous_binding is None:
        raise RuntimeError("context-seal-binding-required: applied assembly 缺少 epoch binding")
    if previous_binding is None:
        state = initial_epoch_state(
            provider_profile=provider_profile, tool_compatibility_key=compatibility_key,
            turn_id=turn_id, transition=transition,
        )
    elif transition is not None or compatibility_key != previous_binding.prefix.state.tool_compatibility_key:
        state = open_rebuild_epoch(
            previous_binding.prefix.state,
            reason=transition.reason if transition is not None else EpochReason.TOOLSET_CHANGED,
            provider_profile=provider_profile, tool_compatibility_key=compatibility_key,
            transition=transition, turn_id=turn_id,
        )
    else:
        state = previous_binding.prefix.state
    rebuilding = not state.epoch_applied
    by_id = {ref.ref_id: ref for ref in plan.refs}
    by_contribution = {source.contribution_id: source for source in plan.contributions}
    selected_ids: list[str] = []
    root = previous_binding.root if previous_binding is not None else None
    frames: list[ProviderProfileItemFrame] = []
    if rebuilding:
        candidates = []
        if retained_source_assembly is not None:
            retained_binding = retained_source_assembly.seal_binding
            if retained_binding is None:
                raise RuntimeError("checkpoint-context-state-conflict: retained source 缺少 immutable prefix")
            for provenance in (() if retained_binding.root is None else retained_binding.root.sources):
                matches = tuple(
                    source for source in retained_source_assembly.contributions
                    if source.source_binding is not None
                    and source.source_binding.source_ref == provenance.source_identity
                    and source.source_revision == provenance.revision
                    and source.root_placement == "root_eligible"
                )
                if len(matches) != 1:
                    raise RuntimeError("checkpoint-context-state-conflict: retained root 缺少唯一 selected source")
                root_refs = tuple(
                    entry.ref.ref_id for entry in retained_source_assembly.selection
                    if entry.included and entry.ref.ref_type == "request_only"
                    and entry.contribution_id == matches[0].contribution_id
                )
                if len(root_refs) != 1:
                    raise RuntimeError("checkpoint-context-state-conflict: retained root 缺少唯一 source ref")
                if (root_refs[0], matches[0].source_revision) not in retained_source_refs:
                    continue
                contribution = by_contribution[matches[0].contribution_id]
                content = (retained_source_bodies or {}).get(contribution.contribution_id)
                if not isinstance(content, str) or contribution.source_ordinal != provenance.source_ordinal:
                    raise RuntimeError("checkpoint-context-state-conflict: retained root detail/ordinal 不一致")
                candidates.append(RootCandidateSource(
                    source_identity=provenance.source_identity, source_kind=provenance.source_kind,
                    name=provenance.name, revision=provenance.revision, content=content,
                    source_ordinal=contribution.source_ordinal, root_placement="root_eligible",
                    included_reason="epoch_rebuild",
                ))
                selected_ids.append(root_refs[0])
        for source in batch.sources:
            if source.descriptor.root_placement != "root_eligible" or not source.content:
                continue
            contribution = by_contribution[full_source_id(source)]
            if contribution.source_ordinal is None:
                raise RuntimeError("root compiler 缺少 registry 分配的 source ordinal")
            candidates.append(RootCandidateSource(
                source_identity=source.descriptor.source_id,
                source_kind=source.descriptor.source_kind,
                name=source.descriptor.name,
                revision=source.revision, content=source.content,
                source_ordinal=contribution.source_ordinal,
                root_placement=source.descriptor.root_placement,
                included_reason=(
                    "toolset_policy" if source.descriptor.policy_key is not None
                    else "initial" if state.epoch.reason == EpochReason.INITIAL else "epoch_rebuild"
                ),
            ))
            selected_ids.append(contribution.contribution_id)
        root = compile_root_item(state, candidates)
        if root is not None:
            frames.append(ProviderProfileItemFrame(
                1, provider_profile, "system", "text", root.content,
                AppendedItemRef(
                    "source_item", f"root:{root.epoch.token}", "compiled-root",
                    root.frame_hash(), "tracked",
                ),
            ))
    elif previous is not None:
        for entry in previous.selection:
            if entry.included and entry.ref.ref_type != "tool_set":
                if entry.ref.ref_id not in by_id:
                    raise RuntimeError(
                        "stable-prefix-violation: canonical/source 消失而没有真实 rebuild boundary: "
                        + entry.ref.ref_id
                    )
                selected_ids.append(entry.ref.ref_id)
    inherited = set(selected_ids)
    canonical_items = [
        item for item in items
        if item.item_id in by_id and item.item_id not in inherited
    ]
    # 真实 terminal outcomes 全部选入之后才追加来源通知；不以物理 append 交错排序。
    canonical_items = [
        item for item in canonical_items if item.semantic_kind != "runtime_notice"
    ] + [
        item for item in canonical_items if item.semantic_kind == "runtime_notice"
    ]
    canonical_frames, canonical_ids = project_canonical_prefix_frames(
        canonical_items, provider_profile=provider_profile,
        start_ordinal=len(state.frames) + len(frames),
    )
    frames.extend(canonical_frames)
    selected_ids.extend(canonical_ids)
    ordinal = len(state.frames) + len(frames)
    if rebuilding and retained_source_assembly is not None:
        # 携带 cutoff 前已封存的 snapshot/untracked 帧；不生成 CSM 恢复决策。
        for frame in retained_source_assembly.seal_binding.prefix.state.frames:
            if (frame.item_ref.ref_id, frame.item_ref.source_revision) not in retained_source_refs or frame.role != "user":
                continue
            ref_id = frame.item_ref.ref_id
            if ref_id not in by_id:
                raise RuntimeError("checkpoint-context-state-conflict: retained source ref 已丢失")
            ordinal += 1
            selected_ids.append(ref_id)
            frames.append(replace(
                frame, ordinal=ordinal, provider_profile=provider_profile,
                item_ref=replace(frame.item_ref, tracking_state="not_tracked"),
            ))
    source_by_id = {source.descriptor.source_id: source for source in batch.sources}
    tail_values = (
        [
            (full_source_id(source), source, source.content)
            for source in batch.sources
            if source.descriptor.root_placement == "tail_only" or not source.content
        ]
        if rebuilding else [
            (f"source-update:{delta.source_id}:{delta.revision}:{delta.kind}",
             source_by_id[delta.source_id], delta.content)
            for delta in batch.deltas
        ]
    )
    for ref_id, source, content in tail_values:
        ordinal += 1
        selected_ids.append(ref_id)
        frames.append(ProviderProfileItemFrame(
            ordinal, provider_profile, "user", "text", content,
            AppendedItemRef(
                "source_item", ref_id, source.descriptor.source_id, source.revision,
                "tracked" if source.tracked else "not_tracked",
            ),
        ))
    # 持久 overlay 是独立的 post-user 来源；沿已有 typed ref 的 base/delta 顺序追加。
    for ref in plan.refs:
        if ref.base_delta_role not in {"base", "delta"} or ref.ref_id in inherited:
            continue
        selected_ids.append(ref.ref_id)
        if ref.availability != "available":
            continue
        contribution = resolve_contribution_for_ref(ref, plan.contributions)
        if contribution is None or contribution.source_binding is None:
            raise ValueError("source-mismatch: active overlay 缺少唯一 source binding")
        ordinal += 1
        frames.append(ProviderProfileItemFrame(
            ordinal, provider_profile, "user", "structured_content",
            overlay_bodies[ref.ref_id],
            AppendedItemRef(
                "source_item", ref.ref_id, contribution.source_binding.source_ref,
                contribution.source_revision, "tracked",
            ),
        ))
    state = append_items(
        state, frames, desired_provider_profile=provider_profile,
        desired_tool_compatibility_key=compatibility_key,
    )
    selected_refs = tuple(by_id[ref_id] for ref_id in selected_ids)
    selected_contribution_ids = {
        contribution.contribution_id
        for ref in selected_refs
        if (contribution := resolve_contribution_for_ref(ref, plan.contributions)) is not None
    }
    selected_plan = replace(
        plan, refs=selected_refs,
        contributions=tuple(
            source for source in plan.contributions
            if source.contribution_id in selected_contribution_ids
        ),
    )
    preparation = ContextSealPreparation(
        prefix_state=state, root=root, tool_set=tool_set,
        previous_assembly_id=previous.assembly_id if previous is not None else None,
        source_revisions=(
            tuple(AppliedSourceRevision(
                source.descriptor.source_id, source.revision,
                f"source-rebuild:{transition.transition_id}:{source.descriptor.source_id}",
                source.applied_revision,
                expected_control_revision=source.expected_control_revision,
            ) for source in batch.sources)
            if transition is not None else tuple(AppliedSourceRevision(
                delta.source_id, delta.revision,
                f"source-seal:{delta.source_id}:{delta.revision}:{delta.kind}",
                delta.previous_revision,
            ) for delta in batch.deltas)
        ),
        transition=transition,
    )
    return selected_plan, preparation
