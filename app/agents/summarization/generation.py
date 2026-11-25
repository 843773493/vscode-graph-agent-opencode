"""冻结来源的完整摘要：按真实请求预算分块，沿唯一模型 owner 发送和合并。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.agents.model_call_execution import DerivedModelRequest, ModelCallExecution
from app.agents.model_call_graph import run_derived_agent
from app.agents.summarization.retry import (
    build_summary_instruction,
    validate_summary_text,
)
from app.agents.summarization.state import (
    CompactionReductionError,
    SummaryGenerationError,
)
from app.core.execution_admission_context import execution_admission_scope
from app.core.failures import is_provider_failure
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.context_compaction import (
    ContextBudget,
    ContextCapacityError,
    ModelCapacity,
)
from app.domain.itemized.compaction import (
    CanonicalTextRange,
    FrozenCompactionRange,
    FrozenSummaryResult,
)
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    InternalExecutionIntent,
)
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef, ref_identity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
    closed_source_boundaries,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_source_groups,
)
from app.services.mapping.itemized.prefix_frames import project_text_range
from app.services.orchestration.resource_activation import ResourceActivationCoordinator

MAX_DERIVED_CALLS = 32
MAX_MERGE_LEVELS = 3


@dataclass(slots=True)
class SummaryCallBudget:
    """原任务本次运行的预算值；接管必须传回同一累计值，不能逐块重置期限。"""

    deadline: float
    calls_used: int = 0
    failure_retries_used: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline):
            raise ValueError("摘要绝对期限必须有限")
        if type(self.calls_used) is not int or not 0 <= self.calls_used <= MAX_DERIVED_CALLS:
            raise ValueError("摘要累计调用次数必须处于0至32")
        if type(self.failure_retries_used) is not int or self.failure_retries_used not in (0, 1):
            raise ValueError("摘要累计外部失败重试次数必须是0或1")

    def require_calls(self, count: int) -> None:
        if self.calls_used + count > MAX_DERIVED_CALLS:
            raise SummaryGenerationError("compaction-call-budget-exhausted: 完整覆盖预计超过32次派生调用")
        if time.time() >= self.deadline:
            raise TimeoutError("compaction-deadline-exceeded: 摘要任务绝对期限已到")


@dataclass(frozen=True, slots=True)
class _Sources:
    items: tuple[CanonicalItemRecord, ...] = ()
    refs: tuple[ContextRef, ...] = ()
    summaries: tuple[FrozenSummaryResult, ...] = ()
    text_range: CanonicalTextRange | None = None


@dataclass(frozen=True, slots=True)
class _PlannedSummary:
    reference: DerivedExecutionReference
    sources: _Sources
    plan: ContextRequestPlan
    preparation: ContextSealPreparation
    bodies: dict[str, object]
    budget: ContextBudget
    stage: Literal["chunk", "merge"]


class CompactionGenerator:
    """只生成候选，不切 view；后台和阻断入口使用相同冻结模型与累计预算。"""

    def __init__(
        self, *, saver: RolloutCheckpointSaver, model: BaseChatModel,
        activation: ResourceActivationCoordinator, provider_wait: Callable[[bool], None] | None = None,
        initial_sources: Sequence[ContextContribution] = (), initial_tool_set: ToolSetSnapshot | None = None,
        trace_sink: TraceEventSinkProtocol | None = None,
        target_constraint: Mapping[str, object] | None = None,
    ) -> None:
        if getattr(model, "invocation_purpose", None) != "compaction":
            raise ValueError("摘要生成必须使用已冻结的 compaction 模型，禁用隐藏 SDK 重试")
        self._saver = saver
        self._model = model
        self._activation = activation
        self._provider_wait = provider_wait
        self._trace_sink = trace_sink
        self._target_constraint = target_constraint
        self._initial_sources, self._initial_tool_set = tuple(initial_sources), initial_tool_set
        self._capacity = ModelCapacity(
            max_input_tokens=(model.profile or {}).get("max_input_tokens"),
            context_window=(model.profile or {}).get("context_window"),
        )

    async def generate(
        self, frozen: FrozenCompactionRange, *, budget: SummaryCallBudget,
        derived_summaries: Sequence[FrozenSummaryResult] = (), allow_external_retry: bool = False,
        publication_gate: AbstractAsyncContextManager[None] | None = None,
    ) -> FrozenSummaryResult:
        """完整结果先发布到 rollout；原 task 的完成确认由调度 owner 随后执行。"""
        budget.require_calls(0)
        # 一个绝对期限包住规划、全部网络调用、合并和发布，不为每块重置 timeout。
        async with asyncio.timeout(budget.deadline - time.time()):
            return await self._generate(frozen, budget, tuple(derived_summaries), allow_external_retry, publication_gate)

    async def _generate(
        self, frozen: FrozenCompactionRange, budget: SummaryCallBudget,
        summaries: tuple[FrozenSummaryResult, ...], allow_external_retry: bool,
        publication_gate: AbstractAsyncContextManager[None] | None,
    ) -> FrozenSummaryResult:
        candidates = self._saver._storage.compactions
        ready = await run_sync_shielded(
            candidates.read_result, frozen.session_id, frozen.thread_id, frozen.candidate_id,
            checkpoint_ns=frozen.checkpoint_ns,
        )
        if ready is not None:
            return ready
        covered = tuple(ref for summary in summaries for ref in summary.coverage)
        if frozen.refs[:len(covered)] != covered:
            raise ValueError("compaction-coverage-mismatch: 前序摘要必须覆盖冻结范围的有序前缀")
        refs = frozen.refs[len(covered):]
        materialized = await run_sync_shielded(
            self._saver._storage.storage.read_ref_items, frozen.session_id, frozen.thread_id,
            refs=refs, checkpoint_ns=frozen.checkpoint_ns,
        ) if refs else {}
        items = tuple(materialized[ref_identity(ref)] for ref in refs)
        ends = closed_source_boundaries(items) if items else ()
        units = [_Sources(summaries=(summary,)) for summary in summaries]
        start = 0
        for end in ends:
            units.append(_Sources(items[start:end], refs[start:end]))
            start = end
        if start != len(items) or not units:
            raise ValueError("compaction-source-unavailable: 冻结范围没有完整闭合的摘要边界")
        owner = ModelCallExecution(checkpointer=self._saver, session_id=frozen.session_id, thread_id=frozen.thread_id)
        chunks = await run_sync_shielded(self._pack, frozen, owner, units, stage="chunk", level=0)
        # 至少还需要一次完整合并；超预算时整项拒绝，不能先发送前32块。
        pending = await run_sync_shielded(candidates.remaining_calls, frozen, [request.reference.plan_id for request in chunks])
        budget.require_calls(pending + (1 if len(chunks) > 1 else 0))
        results = [await self._invoke(frozen, owner, request, budget, allow_external_retry) for request in chunks]
        for level in range(1, MAX_MERGE_LEVELS + 1):
            if len(results) == 1:
                break
            merged = await run_sync_shielded(
                self._pack, frozen, owner, [_Sources(summaries=(result,)) for result in results], stage="merge", level=level,
            )
            if len(merged) >= len(results):
                raise CompactionReductionError("compaction-merge-cannot-reduce: 摘要模型无法合并任何相邻结果")
            pending = await run_sync_shielded(candidates.remaining_calls, frozen,
                [request.reference.plan_id for request in merged if len(request.sources.summaries) > 1])
            budget.require_calls(pending + (1 if len(merged) > 1 else 0))
            next_results = []
            for request in merged:
                if len(request.sources.summaries) == 1:
                    # 本层单独剩余的结果直接保留，不重复摘要、不消耗调用预算。
                    next_results.append(request.sources.summaries[0])
                    continue
                result = await self._invoke(frozen, owner, request, budget, allow_external_retry)
                if len(result.text.encode("utf-8")) >= sum(len(source.text.encode("utf-8")) for source in request.sources.summaries):
                    raise CompactionReductionError("compaction-merge-did-not-reduce: 合并结果没有缩短输入摘要")
                next_results.append(result)
            results = next_results
        if len(results) != 1:
            raise SummaryGenerationError("compaction-merge-depth-exhausted: 三层合并后仍不能完整合并")
        result = results[0]
        if result.coverage != frozen.refs or result.text_ranges:
            raise ValueError("compaction-coverage-mismatch: 最终摘要未按顺序覆盖全部冻结来源")
        # 只比较冻结来源的安全正文；不把未压缩的 root、工具定义和指令当成缩减收益。
        source_bytes = sum(len(summary.text.encode("utf-8")) for summary in summaries)
        for group in summary_source_groups(items).values():
            message = project_summary_source(group)
            source_bytes += len(message.content.encode("utf-8")) if isinstance(message.content, str) else len(canonical_json_bytes(message.content))
            if isinstance(message, AIMessage) and message.tool_calls:
                source_bytes += len(canonical_json_bytes(message.tool_calls))
        if len(result.text.encode("utf-8")) >= source_bytes:
            raise CompactionReductionError("compaction-summary-did-not-reduce: 完整摘要没有缩短冻结输入")
        budget.require_calls(0)
        async with publication_gate if publication_gate is not None else nullcontext():
            budget.require_calls(0)
            await run_sync_shielded(candidates.publish_result, frozen, output_refs=result.output_refs,
                                    dispatch_ref=result.dispatch_ref, completion_deadline=datetime.fromtimestamp(budget.deadline, UTC))
        return result

    @staticmethod
    def _combine(units: Sequence[_Sources]) -> _Sources:
        return _Sources(tuple(item for unit in units for item in unit.items),
                        tuple(ref for unit in units for ref in unit.refs),
                        tuple(summary for unit in units for summary in unit.summaries))

    def _plan(
        self, frozen: FrozenCompactionRange, owner: ModelCallExecution, sources: _Sources,
        *, stage: Literal["chunk", "merge"], level: int, ordinal: int,
    ) -> _PlannedSummary:
        identity = sha256_jcs({
            "candidate_id": frozen.candidate_id, "stage": stage, "level": level, "ordinal": ordinal,
            "refs": [ref.to_dict() for ref in sources.refs],
            "results": [result.result_ref.to_dict() for result in sources.summaries],
            "text_range": None if sources.text_range is None else sources.text_range.to_dict(),
        }).rsplit(":", 1)[1]
        reference = DerivedExecutionReference(frozen.task_id, f"summary-{identity}")
        plan, preparation, bodies = owner.compile_summary_request(
            reference=reference, checkpoint_ns=frozen.checkpoint_ns, model=self._model,
            instruction=build_summary_instruction(stage, self._target_constraint).content, items=sources.items, source_refs=sources.refs,
            derived_summaries=sources.summaries, source_assembly_id=frozen.source_assembly_id,
            text_range=sources.text_range,
            initial_sources=self._initial_sources, initial_tool_set=self._initial_tool_set,
        )
        request = owner.preview_summary_request(
            plan=plan, preparation=preparation, bodies=bodies, items=sources.items, source_refs=sources.refs,
            derived_summaries=sources.summaries, model=self._model, checkpoint_ns=frozen.checkpoint_ns,
        )
        return _PlannedSummary(reference, sources, plan, preparation, bodies, request.budget(self._capacity), stage)

    def _pack(
        self, frozen: FrozenCompactionRange, owner: ModelCallExecution, units: Sequence[_Sources],
        *, stage: Literal["chunk", "merge"], level: int,
    ) -> list[_PlannedSummary]:
        """按真实 wire 预算选择闭合边界；仅独立纯文本允许完整有序分片。"""
        requests = []
        start = 0
        while start < len(units):
            left, right = start + 1, len(units)
            best = None
            while left <= right:
                end = (left + right) // 2
                request = self._plan(frozen, owner, self._combine(units[start:end]), stage=stage, level=level, ordinal=len(requests))
                if request.budget.exceeds_capacity:
                    right = end - 1
                else:
                    best = (end, request)
                    left = end + 1
            if best is None:
                request = self._plan(frozen, owner, units[start], stage=stage, level=level, ordinal=len(requests))
                source = units[start]
                if (stage != "chunk" or len(source.items) != 1 or source.summaries
                        or source.items[0].payload_kind != "text"
                        or source.items[0].semantic_kind not in {"user_input", "assistant_output"}
                        or source.refs[0].protection != "public" or not isinstance(source.items[0].payload, str)):
                    raise ContextCapacityError(request.budget)
                pending = [CanonicalTextRange(source.refs[0], 0, len(source.items[0].payload), len(source.items[0].payload))]
                while pending:
                    part = pending.pop()
                    # 复用冷读取相同的安全投影，禁止把 reasoning／工具 JSON 转成可切文本。
                    project_text_range(source.items[0], part)
                    sliced = replace(source, text_range=part)
                    request = self._plan(frozen, owner, sliced, stage=stage, level=level, ordinal=len(requests))
                    if request.budget.exceeds_capacity:
                        if part.end - part.start == 1:
                            raise ContextCapacityError(request.budget)
                        middle = (part.start + part.end) // 2
                        pending.extend((replace(part, start=middle), replace(part, end=middle)))
                    else:
                        requests.append(request)
                    if len(requests) + len(pending) > MAX_DERIVED_CALLS:
                        raise SummaryGenerationError("compaction-call-budget-exhausted: 完整分片预计超过32次派生调用")
                start += 1
                continue
            start, request = best
            requests.append(request)
        return requests

    async def _invoke(
        self, frozen: FrozenCompactionRange, owner: ModelCallExecution, request: _PlannedSummary,
        budget: SummaryCallBudget, allow_external_retry: bool,
    ) -> FrozenSummaryResult:
        while True:
            reference = request.reference
            identity = reference.plan_id
            prior = await run_sync_shielded(self._saver._storage.compactions.prior_request, frozen, identity)
            if prior is not None:
                outcome, result = prior
                if result is not None:
                    return result
                if outcome != "failed" or not allow_external_retry or budget.failure_retries_used:
                    raise SummaryGenerationError(f"compaction-prior-request-terminal: 原请求不可重放，outcome={outcome}")
                budget.failure_retries_used += 1
                request = await run_sync_shielded(self._retry_request, frozen, owner, request)
                continue
            budget.require_calls(1)
            admission = await run_sync_shielded(
                self._saver.admit_internal_execution, frozen.session_id, thread_id=frozen.thread_id,
                job_id=f"summary-job:{identity}", message=None, checkpoint_ns=frozen.checkpoint_ns,
                intent=InternalExecutionIntent(message_id=f"summary-message:{identity}", execution_id=f"summary-execution:{identity}",
                                               admission_idempotency_key=f"summary-admission:{identity}", derived_reference=reference),
            )
            with (
                session_thread_owner_scope(MutationIntentOwner(frozen.session_id, frozen.thread_id), langgraph_thread_id=frozen.thread_id),
                execution_admission_scope(admission, langgraph_thread_id=frozen.thread_id),
            ):
                activation = await self._activation.freeze_execution(
                    admission=admission, policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}),
                    checkpoint_ns=frozen.checkpoint_ns,
                )
                budget.calls_used += 1
                try:
                    response = await run_derived_agent(DerivedModelRequest(
                        reference, admission, frozen.checkpoint_ns, self._model, request.plan, request.preparation,
                        request.bodies, activation, str(UUID(hex=create_uuid_hex(namespace=f"model_call/{frozen.session_id}/{frozen.thread_id}"))),
                        provider_wait=self._provider_wait,
                    ), execution=owner, max_model_calls=1, completion_deadline=datetime.fromtimestamp(budget.deadline, UTC),
                        trace_sink=self._trace_sink)
                except Exception as error:
                    if not is_provider_failure(error) or not allow_external_retry or budget.failure_retries_used:
                        raise
                    budget.failure_retries_used += 1
                    request = await run_sync_shielded(self._retry_request, frozen, owner, request)
                    continue
            validate_summary_text(response.response.result[0].text)
            return await run_sync_shielded(
                self._saver._storage.compactions.save_summary_result, frozen,
                output_refs=response.output_refs, dispatch_ref=response.dispatch_ref,
            )

    def _retry_request(self, frozen: FrozenCompactionRange, owner: ModelCallExecution, request: _PlannedSummary) -> _PlannedSummary:
        """补尝试保持模型、资料和指令，只使用唯一第二次请求身份。"""
        reference = replace(request.reference, plan_id=f"{request.reference.plan_id}-retry")
        plan, preparation, bodies = owner.compile_summary_request(
            reference=reference, checkpoint_ns=frozen.checkpoint_ns, model=self._model,
            instruction=build_summary_instruction(request.stage, self._target_constraint).content, items=request.sources.items,
            source_refs=request.sources.refs, derived_summaries=request.sources.summaries, source_assembly_id=frozen.source_assembly_id,
            text_range=request.sources.text_range,
            initial_sources=self._initial_sources, initial_tool_set=self._initial_tool_set,
        )
        return replace(request, reference=reference, plan=plan, preparation=preparation, bodies=bodies)
