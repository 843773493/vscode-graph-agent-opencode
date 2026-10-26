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
    RuntimeNoticeAcceptanceRef,
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
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.plans.contributions import (
    ContextContribution,
    SourceBinding,
    resolve_contribution_for_ref,
)
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.business.resource_derivations.team import TEAM_SOURCE_IDS
from app.services.infrastructure.mcp.catalog_activation import MCP_GUIDANCE_SOURCE_ID
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
    initial_assembly: bool = False,
    transition: PendingPrefixEpochTransition | None = None,
    retained_source_assembly: ContextAssemblySnapshot | None = None,
    retained_source_refs: frozenset[tuple[str, str]] = frozenset(),
    retained_source_bodies: Mapping[str, object] | None = None,
    notice_acceptance: RuntimeNoticeAcceptanceRef | None = None,
    allow_provider_reprojection: bool = False,
) -> tuple[ContextRequestPlan, ContextSealPreparation]:
    """同 epoch 精确继承已封存 prefix，只在真实边界重编 root。"""
    tool_ref = plan.tool_set_refs[0]
    compatibility_key = tool_set.compatibility_key(tool_ref)
    previous_binding = previous.seal_binding if previous is not None else None
    provider_reprojection = False
    if previous is not None and previous_binding is None:
        raise RuntimeError("context-seal-binding-required: applied assembly 缺少 epoch binding")
    if previous_binding is None or initial_assembly:
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
    elif (
        allow_provider_reprojection
        and provider_profile != previous_binding.prefix.state.provider_profile
    ):
        # 只有路由本次明确选中的候选可授权换投影；未授权的漂移仍由 append 拒绝。
        state = open_rebuild_epoch(
            previous_binding.prefix.state, reason=EpochReason.PROVIDER_CHANGED,
            provider_profile=provider_profile, tool_compatibility_key=compatibility_key,
            turn_id=turn_id,
        )
        provider_reprojection = True
    else:
        state = previous_binding.prefix.state
    rebuilding = not state.epoch_applied and not provider_reprojection
    source_rebuild = transition is not None and transition.reason in {EpochReason.REWIND, EpochReason.COMPACTION}
    if rebuilding and not source_rebuild and previous_binding is not None and not initial_assembly:
        # ToolSet/notice rebase 不修改 source 状态；已封存的 untracked 用户来源仍属于
        # 当前 owner 的有效上下文。复用真实 selection/frame，不重新观察来源。
        retained_source_assembly = previous
        retained_source_refs = frozenset(
            (frame.item_ref.ref_id, frame.item_ref.source_revision)
            for frame in previous_binding.prefix.state.frames
            if frame.role == "user" and frame.item_ref.ref_type == "source_item"
            and frame.item_ref.source_tracking_at_seal == "untracked"
        )
        if transition is not None and transition.reason == EpochReason.RUNTIME_NOTICE_CONSUMED:
            retained_source_refs |= frozenset(
                (frame.item_ref.ref_id, frame.item_ref.source_revision)
                for frame in previous_binding.prefix.state.frames
                if frame.role == "user" and frame.item_ref.ref_type == "source_item"
            )
            prior_roots = {(source.source_identity, source.revision) for source in (
                () if previous_binding.root is None else previous_binding.root.sources
            ) if source.included_reason != "toolset_policy"}
            retained_source_refs |= frozenset(
                (entry.ref.ref_id, source.source_revision)
                for entry in previous.selection if entry.included
                and (source := resolve_contribution_for_ref(entry.ref, previous.contributions)) is not None
                and source.source_binding is not None and source.root_placement == "root_eligible"
                and (source.source_binding.source_ref, source.source_revision) in prior_roots
            )
    if any(ref.ref_type == "canonical_item" and ref.thread_id != plan.thread_id for ref in plan.refs):
        raise ValueError("source-mismatch: stable prefix 不得跨 thread 拼接 canonical refs")
    by_identity = {selection_ref_identity(ref): ref for ref in plan.refs}
    # prefix wire 的 local ID 只在当前 owner thread 内解析。
    by_contribution = {source.contribution_id: source for source in plan.contributions}
    selected_identities: list[tuple[str, ...]] = []
    root = previous_binding.root if previous_binding is not None else None
    if provider_reprojection:
        # 复用原封存的正文、角色、身份与顺序；不重新观察来源或重编 root。
        if root is not None:
            root = replace(root, epoch=state.epoch, provider_profile=provider_profile)
        state = replace(state, frames=tuple(replace(
            frame, provider_profile=provider_profile,
            item_ref=AppendedItemRef(
                "source_item", f"root:{root.epoch.token}", "compiled-root", root.frame_hash(), "tracked",
            ) if frame.role == "system" and root is not None else frame.item_ref,
        ) for frame in previous_binding.prefix.state.frames))
    frames: list[ProviderProfileItemFrame] = []
    if rebuilding:
        candidates: dict[str, tuple[RootCandidateSource, tuple[str, ...]]] = {}
        observed = {source.descriptor.source_id for source in batch.sources}
        if retained_source_assembly is not None:
            retained_binding = retained_source_assembly.seal_binding
            if retained_binding is None:
                raise RuntimeError("checkpoint-context-state-conflict: retained source 缺少 immutable prefix")
            provenance = {
                (source.source_identity, source.revision): source
                for source in (() if retained_binding.root is None else retained_binding.root.sources)
            }
            for entry in retained_source_assembly.selection:
                source = resolve_contribution_for_ref(entry.ref, retained_source_assembly.contributions)
                if (not entry.included or source is None or source.source_binding is None
                        or source.contribution_kind != "prompt" or source.root_placement != "root_eligible"
                        or (entry.ref.ref_id, source.source_revision) not in retained_source_refs):
                    continue
                identity = source.source_binding.source_ref
                if identity in observed:
                    continue
                contribution = by_contribution[source.contribution_id]
                content = (retained_source_bodies or {}).get(contribution.contribution_id)
                prior = provenance.get((identity, source.source_revision))
                if (not isinstance(content, str) or contribution.source_ordinal is None
                        or (prior is not None and contribution.source_ordinal != prior.source_ordinal)):
                    raise RuntimeError("checkpoint-context-state-conflict: retained root detail/ordinal 不一致")
                if not content:
                    continue
                # 只提升已选中的完整 typed 来源；notice/delta 仍保留为 user。
                candidates[identity] = (RootCandidateSource(
                    source_identity=identity, source_kind=source.source_kind,
                    name=prior.name if prior is not None else identity,
                    revision=source.source_revision, content=content,
                    source_ordinal=contribution.source_ordinal, root_placement="root_eligible",
                    included_reason="initial" if state.epoch.reason == EpochReason.INITIAL else "epoch_rebuild",
                ), ("request_only", entry.ref.ref_id, plan.thread_id))
        for source in batch.sources:
            if source.descriptor.root_placement != "root_eligible" or not source.content:
                continue
            contribution = by_contribution[full_source_id(source)]
            if contribution.source_ordinal is None:
                raise RuntimeError("root compiler 缺少 registry 分配的 source ordinal")
            candidates[source.descriptor.source_id] = (RootCandidateSource(
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
            ), ("request_only", contribution.contribution_id, plan.thread_id))
        selected_identities.extend(identity for _, identity in candidates.values())
        root = compile_root_item(state, [candidate for candidate, _ in candidates.values()])
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
                identity = selection_ref_identity(entry.ref)
                if identity not in by_identity:
                    raise RuntimeError(
                        "stable-prefix-violation: canonical/source 消失而没有真实 rebuild boundary: "
                        + entry.ref.ref_id
                    )
                selected_identities.append(identity)
    inherited = set(selected_identities)
    canonical_items = [
        item for item in items
        if ("canonical_item", item.item_id, plan.thread_id) in by_identity
        and ("canonical_item", item.item_id, plan.thread_id) not in inherited
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
    selected_identities.extend(("canonical_item", item_id, plan.thread_id) for item_id in canonical_ids)
    source_by_id = {source.descriptor.source_id: source for source in batch.sources}
    # TODO(CIL 9.6): 用统一来源块显式表达完整替换及跨来源替代范围，迁齐历史投影与重放。
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
    current_tail = {(source.descriptor.source_id, source.revision): content
                    for _, source, content in tail_values} if rebuilding else {}
    ordinal = len(state.frames) + len(frames)
    if rebuilding and retained_source_assembly is not None:
        # 携带 cutoff 前已封存的 snapshot/untracked 帧；不生成 CSM 恢复决策。
        for frame in retained_source_assembly.seal_binding.prefix.state.frames:
            if frame.item_ref.source_identity in {MCP_GUIDANCE_SOURCE_ID, *TEAM_SOURCE_IDS.values()}:
                continue
            ref_identity = ("request_only", frame.item_ref.ref_id, plan.thread_id)
            if ((frame.item_ref.ref_id, frame.item_ref.source_revision) not in retained_source_refs
                    or frame.role != "user" or ref_identity in inherited):
                continue
            if ref_identity not in by_identity:
                raise RuntimeError("checkpoint-context-state-conflict: retained source ref 已丢失")
            source = resolve_contribution_for_ref(by_identity[ref_identity], plan.contributions)
            identity = (frame.item_ref.source_identity, frame.item_ref.source_revision)
            if source is not None and source.contribution_kind == "prompt" and identity in current_tail:
                if frame.content != current_tail[identity]:
                    raise RuntimeError("source-identity-conflict: 同一 tail source revision 的完整正文不一致")
                # fork 本地化后的旧 ref 与当前完整来源只能贡献一次，异 revision 历史仍携带。
                continue
            ordinal += 1
            selected_identities.append(ref_identity)
            frames.append(replace(
                frame, ordinal=ordinal, provider_profile=provider_profile,
                item_ref=replace(frame.item_ref, source_tracking_at_seal="untracked"),
            ))
    for ref_id, source, content in tail_values:
        ordinal += 1
        selected_identities.append(("request_only", ref_id, plan.thread_id))
        frames.append(ProviderProfileItemFrame(
            ordinal, provider_profile, "user", "text", content,
            AppendedItemRef(
                "source_item", ref_id, source.descriptor.source_id, source.revision,
                "tracked" if source.tracked else "untracked",
            ),
        ))
    # 持久 overlay 是独立的 post-user 来源；沿已有 typed ref 的 base/delta 顺序追加。
    inherited = set(selected_identities)
    for ref in plan.refs:
        identity = selection_ref_identity(ref)
        if ref.base_delta_role not in {"base", "delta"} or identity in inherited:
            continue
        selected_identities.append(identity)
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
    selected_refs = tuple(by_identity[identity] for identity in selected_identities)
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
            # tracked rebuild 提交冻结 control CAS；activation 来源只在
            # revision 真正变化时推进 applied，完整帧已由新 epoch 独立封存。
            tuple(AppliedSourceRevision(
                source.descriptor.source_id, source.revision,
                f"source-rebuild:{transition.transition_id}:{source.descriptor.source_id}",
                source.applied_revision,
                expected_control_revision=source.expected_control_revision,
            ) for source in batch.sources if (
                source.expected_control_revision is not None
                or source.revision != source.applied_revision
            ))
            if source_rebuild else tuple(AppliedSourceRevision(
                delta.source_id, delta.revision,
                f"source-seal:{delta.source_id}:{delta.revision}:{delta.kind}",
                delta.previous_revision,
            ) for delta in batch.deltas)
        ),
        transition=transition,
        notice_acceptance=notice_acceptance,
    )
    return selected_plan, preparation
