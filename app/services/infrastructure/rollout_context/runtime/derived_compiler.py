"""冻结来源的派生请求：复用来源 root／tools，补齐闭合尾部后追加任务指令。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from langchain_core.messages import AIMessage, ToolMessage, message_to_dict

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.compaction import CanonicalTextRange
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    ProviderProfileItemFrame,
    append_items,
    initial_epoch_state,
)
from app.domain.itemized.epoch.root_compilation import (
    RootCandidateSource,
    compile_root_item,
)
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.epoch.source_protocol import (
    CURRENT_SOURCE_PROTOCOL_VERSION,
    SourceModelBlock,
    render_source_blocks,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.serde.registry import parse_context_ref
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_source_groups,
)
from app.services.mapping.itemized.prefix_frames import (
    project_canonical_prefix_frames,
    project_text_range,
)


def request_ref(plan: ContextRequestPlan, source: ContextContribution) -> ContextRef:
    return ContextRef.request_only_ref(
        source.contribution_id, session_id=plan.session_id, thread_id=plan.thread_id, plan_id=plan.plan_id,
        source_revision=source.source_revision, semantic_kind="extension", payload_kind="structured_content",
        content_length=source.content_length, content_hash_value=source.content_hash, source_ref=source.contribution_id,
        protection=source.protection, visibility=source.visibility,
    )


def inherited_source_selection(
    snapshot: ContextAssemblySnapshot, source_refs: Sequence[ContextRef], *, replacement_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[ProviderProfileItemFrame, ...], frozenset[str], frozenset[str]]:
    """复用连续 canonical 前缀，并保留同一 seal 后续的来源更新。"""
    binding = snapshot.seal_binding
    if binding is None:
        raise ValueError("派生来源 assembly 缺少冻结 prefix")
    source_ids = {source.contribution_id for source in snapshot.contributions
                  if binding.root is not None and source.source_binding is not None and any(
                      (source.source_binding.source_ref, source.source_revision) == (original.source_identity, original.revision)
                      for original in binding.root.sources)}
    source_refs_by_id = {entry.ref.ref_id: entry.ref for entry in snapshot.as_sealed_plan().selection
                         if entry.included and entry.ref.ref_type == "canonical_item"}
    inherited_ids: set[str] = set()
    frames = []
    canonical_prefix = True
    for frame in binding.prefix.state.frames:
        if frame.role == "system":
            if binding.root is not None:
                frames.append(frame)
            continue
        canonical = tuple(member for member in frame.item_refs if member.ref_type == "canonical_item")
        if canonical:
            if not canonical_prefix:
                continue
            ids = tuple(frame.content["item_ids"]) if isinstance(frame.content, Mapping) else tuple(member.ref_id for member in canonical)
            selected = tuple(source_refs[len(inherited_ids):len(inherited_ids) + len(ids)])
            if (tuple(ref.ref_id for ref in selected) != ids or replacement_ids.intersection(ids)
                    or any(source_refs_by_id.get(ref.ref_id) != ref for ref in selected)):
                canonical_prefix = False
                continue
            inherited_ids.update(ids)
        else:
            source_ids.update(member.ref_id for member in frame.item_refs)
        frames.append(frame)
    return tuple(frames), frozenset(inherited_ids), frozenset(source_ids)


def compile_derived_request(
    *, session_id: str, thread_id: str, plan_id: str, provider_profile: str,
    items: Sequence[CanonicalItemRecord], source_refs: Sequence[ContextRef],
    instruction: ContextContribution, tool_set: ToolSetSnapshot,
    source_snapshot: ContextAssemblySnapshot | None = None,
    source_contributions: Sequence[ContextContribution] = (),
    source_content: Mapping[str, object] | None = None,
    input_messages: Sequence[ContextContribution] = (), backing_refs: Sequence[ContextRef] = (),
    replacement_message: ContextContribution | None = None,
    text_range: CanonicalTextRange | None = None,
) -> tuple[ContextRequestPlan, ContextSealPreparation, dict[str, object]]:
    """只使用传入的冻结材料；不读取 CSM、不消费主来源、不建立父 Graph。"""
    refs = tuple(source_refs)
    if len(refs) != len(items) or any(
        ref != ContextRef.canonical_item(item, session_id=ref.session_id, thread_id=ref.thread_id)
        for ref, item in zip(refs, items, strict=True)
    ):
        raise ValueError("derived-source-mismatch: 原文与冻结引用不一致")
    if text_range is not None and (refs != (text_range.ref,) or input_messages or replacement_message is not None):
        raise ValueError("derived-source-mismatch: 文本分片必须只读唯一原件，不混入其它摘要")
    replacement_refs = (() if replacement_message is None else
                        tuple(parse_context_ref(ref) for ref in replacement_message.body["coverage"]))
    replacement_ids = {ref.ref_id for ref in replacement_refs}
    replacement_start = 0
    if replacement_refs:
        replacement_start = refs.index(replacement_refs[0])
        if refs[replacement_start:replacement_start + len(replacement_refs)] != replacement_refs:
            raise ValueError("derived-source-mismatch: 冻结摘要不是所选范围的完整连续子段")
    state = initial_epoch_state(
        provider_profile=provider_profile,
        tool_compatibility_key=tool_set.compatibility_key(tool_set.bind(
            session_id=session_id, thread_id=thread_id, plan_id=plan_id,
        )),
    )
    root = None
    frames: list[ProviderProfileItemFrame] = []
    inherited_ids: set[str] = set()
    source_ids: set[str] = set()
    if source_snapshot is not None:
        # 上层 reader 已核验目标本地冻结的历史 scope；fork 继承的 seal 保留原 owner。
        binding = source_snapshot.seal_binding
        if binding is None:
            raise ValueError("派生来源 assembly 缺少冻结 prefix")
        if binding.root is not None:
            # 新 execution 使用自己的 epoch；模型可见 root 正文仍逐字复用。
            root = replace(binding.root, epoch=state.epoch, provider_profile=provider_profile,
                           tool_compatibility_key=state.tool_compatibility_key)
        inherited_frames, inherited_ids, selected_source_ids = inherited_source_selection(
            source_snapshot, refs, replacement_ids=frozenset(replacement_ids | ({text_range.ref.ref_id} if text_range is not None else set())),
        )
        source_ids.update(selected_source_ids)
        for frame in inherited_frames:
            if frame.role == "system":
                frames.append(replace(frame, ordinal=len(frames) + 1, provider_profile=provider_profile,
                                      item_refs=(AppendedItemRef("source_item", f"root:{root.epoch.token}",
                                                                "compiled-root", root.frame_hash(), "tracked"),)))
            else:
                frames.append(replace(frame, ordinal=len(frames) + 1, provider_profile=provider_profile))
    elif source_contributions:
        root = compile_root_item(state, [RootCandidateSource(
            source_identity=source.source_binding.source_ref, source_kind=source.source_kind,
            name=source.source_kind, revision=source.source_revision, content=source.body,
            source_ordinal=source.source_ordinal, root_placement=source.root_placement,
            included_reason="initial", source_tracking_at_seal="untracked",
        ) for source in source_contributions if source.root_placement == "root_eligible"])
        if root is not None:
            frames.append(ProviderProfileItemFrame(
                1, provider_profile, "system", "text", root.content,
                (AppendedItemRef("source_item", f"root:{root.epoch.token}", "compiled-root", root.frame_hash(), "untracked"),),
                source_protocol=root.source_protocol,
            ))
        for source in source_contributions:
            source_ids.add(source.contribution_id)
            if source.root_placement == "root_eligible":
                continue
            block = SourceModelBlock(
                source_identity=source.source_binding.source_ref, name=source.source_kind,
                source_kind=source.source_kind, operation="activate", revision=source.source_revision,
                base_revision=None, representation="full", source_tracking_at_seal="untracked",
                root_placement=source.root_placement, body=source.body,
            )
            frames.append(ProviderProfileItemFrame(
                len(frames) + 1, provider_profile, "user", "text", render_source_blocks((block,)),
                (AppendedItemRef("source_item", source.contribution_id, source.source_binding.source_ref,
                                 source.source_revision, "untracked"),), source_protocol=CURRENT_SOURCE_PROTOCOL_VERSION,
            ))
    inherited_sources = tuple(source for source in source_contributions if source.contribution_id in source_ids)
    contributions = (*inherited_sources, *input_messages,
                     *((replacement_message,) if replacement_message is not None else ()), instruction)
    plan = ContextRequestPlan(
        session_id=session_id, thread_id=thread_id, plan_id=plan_id, refs=(),
        contributions=contributions, tool_snapshot=tool_set.tools,
        tool_set_refs=(tool_set.bind(session_id=session_id, thread_id=thread_id, plan_id=plan_id),),
        selection_policy="derived_request",
    )
    bodies = {source.contribution_id: source.body for source in contributions}
    if source_snapshot is not None and source_content is not None:
        bodies.update({source.contribution_id: source_content[source.contribution_id] for source in inherited_sources})
    request_refs = [request_ref(plan, source) for source in inherited_sources]
    def append_message(source: ContextContribution) -> None:
        request_refs.append(request_ref(plan, source))
        message = source.body["message"]
        frames.append(ProviderProfileItemFrame(
            len(frames) + 1, provider_profile, {"human": "user", "ai": "assistant", "tool": "tool"}[message["type"]],
            "structured_content", source.body,
            (AppendedItemRef("source_item", source.contribution_id, source.source_binding.source_ref,
                             source.source_revision, "untracked"),),
        ))
    def append_canonical(selected_items: Sequence[CanonicalItemRecord]) -> None:
        if text_range is not None:
            if len(selected_items) != 1:
                raise ValueError("derived-source-mismatch: 文本分片不是独立单消息")
            item, = selected_items
            message = project_text_range(item, text_range)
            frames.append(ProviderProfileItemFrame(
                len(frames) + 1, provider_profile, {"human": "user", "ai": "assistant"}[message.type],
                "structured_content", {"item_ids": [item.item_id], "message": message_to_dict(message),
                                       "text_range": text_range.to_dict()},
                (AppendedItemRef("canonical_item", item.item_id),),
            ))
        else:
            canonical_frames, _selected_ids = project_canonical_prefix_frames(
                selected_items, provider_profile=provider_profile, start_ordinal=len(frames),
            )
            frames.extend(canonical_frames)

    for source in input_messages:
        append_message(source)
    tail_start = len(inherited_ids)
    if replacement_message is None:
        append_canonical(items[tail_start:])
    else:
        append_canonical(items[tail_start:replacement_start])
        append_message(replacement_message)
        append_canonical(items[replacement_start + len(replacement_refs):])
    request_refs.extend(refs)
    selected_identities = {selection_ref_identity(ref) for ref in request_refs}
    request_refs.extend(ref for ref in backing_refs if selection_ref_identity(ref) not in selected_identities)
    request_refs.append(request_ref(plan, instruction))
    frames.append(ProviderProfileItemFrame(
        len(frames) + 1, provider_profile, "user", "structured_content", instruction.body,
        (AppendedItemRef("source_item", instruction.contribution_id, instruction.source_binding.source_ref,
                         instruction.source_revision, "untracked"),),
    ))
    state = append_items(state, frames, desired_provider_profile=provider_profile,
                         desired_tool_compatibility_key=state.tool_compatibility_key)
    return replace(plan, refs=tuple(request_refs)), ContextSealPreparation(
        prefix_state=state, root=root, tool_set=tool_set, previous_assembly_id=None,
    ), bodies


def closed_source_boundaries(items: Sequence[CanonicalItemRecord]) -> tuple[int, ...]:
    """物理消息完整且工具声明闭合的边界；默认来源允许截在父在途工具之前。"""
    groups = tuple(summary_source_groups(items).values())
    if tuple(item.item_id for group in groups for item in group) != tuple(item.item_id for item in items):
        raise ValueError("derived-source-mismatch: 冻结消息组不连续")
    pending: set[str] = set()
    boundaries = []
    offset = 0
    for group in groups:
        message = project_summary_source(group)
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["id"] in pending:
                    raise ValueError("派生来源重复声明 tool call")
                pending.add(call["id"])
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError("派生来源 tool result 缺少前序声明")
            pending.remove(message.tool_call_id)
        offset += len(group)
        if not pending:
            boundaries.append(offset)
    return tuple(boundaries)
