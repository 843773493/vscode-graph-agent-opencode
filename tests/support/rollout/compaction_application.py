"""真实冻结、摘要生成和采用的 rollout 压缩装具。"""

from __future__ import annotations

import time

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import CheckpointTuple, RunnableConfig

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
    FrozenCompactionRange,
    FrozenSummaryResult,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator


async def generate_and_adopt_compaction_candidate(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    checkpoint_config: RunnableConfig,
    admission: ExecutionAdmission,
    model: BaseChatModel,
    candidate_id: str,
    task_id: str,
    item_ids: tuple[str, ...] | None = None,
    predecessor_candidate_id: str | None = None,
) -> tuple[FrozenCompactionRange, FrozenSummaryResult, CheckpointTuple]:
    """从活动 view 冻结真实范围，调用摘要生成器并原子采用。"""
    frozen, result = await generate_compaction_candidate(
        saver,
        session_id=session_id,
        thread_id=thread_id,
        admission=admission,
        model=model,
        candidate_id=candidate_id,
        task_id=task_id,
        item_ids=item_ids,
        predecessor_candidate_id=predecessor_candidate_id,
    )
    owner = MutationIntentOwner(session_id, thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id), execution_admission_scope(
        admission, langgraph_thread_id=thread_id,
    ):
        with saver._context_reader.open_snapshot(
            session_id, thread_id, frozen.checkpoint_ns,
        ) as snapshot:
            expected_view = saver._storage.checkpoints.active_view_id(snapshot)
        if expected_view is None:
            raise RuntimeError("test-compaction-active-view-missing: 采用前活动 view 不存在")
        adopted = saver.adopt_candidate(
            checkpoint_config,
            ApplyCompactionCandidateIntent(frozen, expected_view),
        )
    return frozen, result, adopted


async def generate_compaction_candidate(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    admission: ExecutionAdmission,
    model: BaseChatModel,
    candidate_id: str,
    task_id: str,
    item_ids: tuple[str, ...] | None = None,
    predecessor_candidate_id: str | None = None,
) -> tuple[FrozenCompactionRange, FrozenSummaryResult]:
    """准备真实冻结与摘要结果，供边界失败测试在采用前截取事务基线。"""
    if (admission.session_id, admission.thread_id) != (session_id, thread_id):
        raise ValueError("test-compaction-owner-mismatch: 准入必须属于候选 SessionThread")
    owner = MutationIntentOwner(session_id, thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id), execution_admission_scope(
        admission, langgraph_thread_id=thread_id,
    ):
        with saver._context_reader.open_snapshot(session_id, thread_id, "") as snapshot:
            source_items = saver._storage.checkpoints.committed_context_items(
                snapshot, predecessor=None, include_pending_notices=False,
            )
        selected_ids = tuple(item.item_id for item in source_items) if item_ids is None else item_ids
        if not selected_ids:
            raise RuntimeError("test-compaction-source-empty: 当前 view 没有可冻结 canonical item")

        frozen = saver._storage.compactions.freeze(
            session_id,
            thread_id,
            candidate_id=candidate_id,
            task_id=task_id,
            item_ids=selected_ids,
            model_config_hash=sha256_jcs({"candidate_model": candidate_id}),
            predecessor_candidate_id=predecessor_candidate_id,
        )
        activation = ResourceActivationCoordinator(
            registry=ResourceRegistry(),
            saver=saver,
            body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
        )
        derived_summaries = ()
        if frozen.predecessor is not None:
            predecessor = saver._storage.compactions.predecessor_result(frozen)
            if predecessor is None:
                raise RuntimeError("test-compaction-predecessor-missing: 冻结候选未恢复已采用前序")
            derived_summaries = (predecessor,)
        result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
            frozen,
            budget=SummaryCallBudget(time.time() + 60),
            derived_summaries=derived_summaries,
        )
        return frozen, result
