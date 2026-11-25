"""只消费冻结来源，为独立摘要请求编译 root、selection 与 provenance。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import (
    message_to_dict,
)

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.compaction import CanonicalTextRange, FrozenSummaryResult
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.plans.contributions import ContextContribution, SourceBinding
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
    ContextSourceSnapshot,
    PendingContextSourceBatch,
)
from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
    closed_source_boundaries,
    compile_derived_request,
)
from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
    source_contributions,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_derived_summary_message,
)


def compaction_history_items(items: Sequence[CanonicalItemRecord]) -> tuple[CanonicalItemRecord, ...]:
    """最新用户输入以前的闭合历史；冻结范围和保留输入预算共用此切点。"""
    last_input = max((index for index, item in enumerate(items) if item.semantic_kind == "user_input"), default=len(items))
    historical = items[:last_input]
    ends = closed_source_boundaries(historical)
    return tuple(historical[:ends[-1]]) if ends else ()


def _instruction_batch(instruction: str) -> PendingContextSourceBatch:
    return PendingContextSourceBatch(deltas=(), sources=(ContextSourceSnapshot(
        descriptor=ContextSourceDescriptor(
            source_id=f"compaction-instruction:{sha256_jcs(instruction)}", source_kind="summarization", name="摘要规则",
            description="本次冻结的独立摘要指令", internal_locator="runtime:compaction-instruction",
            root_placement="tail_only",
        ), revision=sha256_jcs(instruction), content=instruction, applied_revision=None, tracked=False,
    ),))


def summary_request_contributions(
    *, instruction: str, derived_summaries: Sequence[FrozenSummaryResult] = (),
) -> tuple[ContextContribution, ...]:
    """声明冻结正文；实际 source ordinal 由原 request owner 登记。"""
    return (*source_contributions(_instruction_batch(instruction)),
            *(summary_result_contribution(result) for result in derived_summaries))


def summary_result_contribution(result: FrozenSummaryResult) -> ContextContribution:
    """普通派生替换与摘要合并共用同一冻结结果引用和正文投影。"""
    ref_id = f"summary-result:{sha256_jcs(result.result_ref.to_dict())}"
    body = {"message": message_to_dict(project_derived_summary_message(result.text, message_id=ref_id)),
            "result_ref": result.result_ref.to_dict(), **result.detail()}
    return ContextContribution(
        contribution_id=ref_id, source_kind="derived_request", source_revision=sha256_jcs(body),
        contribution_kind="model_message", body=body, content_hash=contribution_content_hash("model_message", body),
        source_binding=SourceBinding(source_ref=ref_id, summary_result_ref=result.result_ref),
        selection_role="backing_only",
    )



def compile_summary_request(
    *, session_id: str, thread_id: str, plan_id: str, provider_profile: str, instruction: str,
    items: Sequence[CanonicalItemRecord], source_refs: Sequence[ContextRef],
    contributions: Sequence[ContextContribution], derived_summaries: Sequence[FrozenSummaryResult] = (),
    source_snapshot: ContextAssemblySnapshot | None = None,
    source_contributions: Sequence[ContextContribution] = (), source_content: Mapping[str, object] | None = None,
    tool_set: ToolSetSnapshot | None = None,
    text_range: CanonicalTextRange | None = None,
) -> tuple[ContextRequestPlan, ContextSealPreparation, dict[str, object]]:
    """压缩只声明块／合并来源，root、tools与指令后缀交共同派生编译器。"""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("摘要请求需要明确的冻结指令")
    if not items and not derived_summaries:
        raise ValueError("摘要请求没有冻结来源")
    if items and (not (boundaries := closed_source_boundaries(items)) or boundaries[-1] != len(items)):
        raise ValueError("摘要来源包含未闭合 tool calls")
    coverage = {}
    for result in derived_summaries:
        if (result.result_ref.session_id, result.result_ref.thread_id) != (session_id, thread_id):
            raise ValueError("summary-source-mismatch: 合并结果不属于本 Thread")
        for ref in (*result.coverage, *result.output_refs):
            identity = selection_ref_identity(ref)
            if identity in coverage and coverage[identity] != ref:
                raise ValueError("summary-source-mismatch: 合并来源版本冲突")
            coverage[identity] = ref
    if any(selection_ref_identity(ref) in coverage for ref in source_refs):
        raise ValueError("summary-source-mismatch: 新原文与前序摘要 coverage 重复")
    instruction_source, = (source for source in contributions if source.contribution_kind == "prompt")
    return compile_derived_request(
        session_id=session_id, thread_id=thread_id, plan_id=plan_id, provider_profile=provider_profile,
        items=items, source_refs=source_refs, instruction=instruction_source,
        tool_set=tool_set if tool_set is not None else ToolSetSnapshot(selection_revision=sha256_jcs({"tools": []}), tools=()),
        source_snapshot=source_snapshot, source_contributions=source_contributions, source_content=source_content,
        input_messages=tuple(source for source in contributions if source.contribution_kind == "model_message"),
        backing_refs=tuple(coverage.values()),
        text_range=text_range,
    )


__all__ = ["compile_summary_request", "summary_request_contributions"]
