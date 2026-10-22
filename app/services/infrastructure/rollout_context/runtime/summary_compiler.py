"""为摘要候选编译独立请求；候选不参与主上下文 applied 状态。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
)

from app.core.model_request_context import SummaryCandidateScope
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    ProviderProfileItemFrame,
    append_items,
)
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.plans.contributions import ContextContribution, SourceBinding
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_retry_variants,
    summary_source_groups,
    validate_summary_source,
)


def compile_summary_candidate(
    plan: ContextRequestPlan,
    preparation: ContextSealPreparation,
    messages: Sequence[BaseMessage],
    items: Sequence[CanonicalItemRecord],
    scope: SummaryCandidateScope,
) -> tuple[tuple[ContextRef, ...], tuple[ContextContribution, ...], ContextSealPreparation, dict[str, object]]:
    """固定候选顺序与角色，原消息身份进入可冷读的 detail provenance。"""
    by_message = summary_source_groups(items)
    retry_groups = []
    for message_id in scope.retry_source_message_ids:
        if message_id not in by_message:
            raise ValueError(f"summary-source-mismatch: 摘要重试中段缺少 canonical 来源: {message_id}")
        retry_groups.append(by_message[message_id])
    retry_ids = [item.item_id for group in retry_groups for item in group] if scope.candidate_ordinal > 0 else []
    variants = summary_retry_variants([project_summary_source(group) for group in retry_groups]) if retry_ids else {}
    refs = list(plan.refs)
    sources = list(plan.contributions)
    frames: list[ProviderProfileItemFrame] = []
    bodies: dict[str, object] = {}
    pending_calls: set[str] = set()
    profile = preparation.prefix_state.provider_profile
    for ordinal, message in enumerate(messages):
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            raise TypeError("摘要候选只接受已声明的 user/assistant/tool 消息")
        group = by_message.get(message.id, []) if message.id is not None else []
        validate_summary_source(message, group, retry_variants=variants.get(message.id, ()))
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = call["id"]
                if call_id in pending_calls:
                    raise ValueError("摘要候选重复声明 tool call")
                pending_calls.add(call_id)
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending_calls:
                raise ValueError("摘要候选 tool result 缺少前序声明")
            pending_calls.remove(message.tool_call_id)
        ref_id = f"summary-message:{plan.plan_id}:{ordinal}"
        body = {"message": message_to_dict(message), "item_ids": [item.item_id for item in group],
                "retry_source_item_ids": retry_ids}
        source = ContextContribution(
            contribution_id=ref_id, source_kind="summary_candidate",
            source_revision=f"{scope.candidate_ordinal}:{scope.retry_phase}",
            contribution_kind="model_message", body=body,
            content_hash=contribution_content_hash("model_message", body),
            source_binding=SourceBinding(source_ref=ref_id),
            selection_role="backing_only",
        )
        refs.append(ContextRef.request_only_ref(
            ref_id, session_id=plan.session_id, thread_id=plan.thread_id,
            plan_id=plan.plan_id, source_revision=source.source_revision,
            semantic_kind="extension", payload_kind="structured_content",
            content_length=source.content_length, content_hash_value=source.content_hash,
            source_ref=ref_id,
        ))
        sources.append(replace(source, body=None))
        bodies[ref_id] = body
        frames.append(ProviderProfileItemFrame(
            len(preparation.prefix_state.frames) + len(frames) + 1, profile,
            {"human": "user", "ai": "assistant", "tool": "tool"}[message.type],
            "structured_content", body,
            AppendedItemRef("source_item", ref_id, ref_id, source.source_revision, "not_tracked"),
        ))
    if pending_calls:
        raise ValueError("摘要候选包含未闭合 tool calls")
    state = append_items(
        preparation.prefix_state, frames, desired_provider_profile=profile,
        desired_tool_compatibility_key=preparation.prefix_state.tool_compatibility_key,
    )
    return (
        tuple(refs), tuple(sources),
        replace(preparation, prefix_state=state, previous_assembly_id=None, source_revisions=(), transition=None),
        bodies,
    )
