"""原压缩运行端口的受理、合并与等待取消，不构造模型或父 Graph。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.context_compaction import CompactionPolicy
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.context_compaction import AgentSummarizationCompactor
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.orchestration.background_work.compaction import CompactionTaskHandler


@pytest.fixture
def compaction_runtime_case():
    owner = MutationIntentOwner("session-runtime", "thread-runtime")
    candidates = SimpleNamespace(freeze=Mock(side_effect=AssertionError("未受理任务不得冻结候选")),
                                 reusable_prefix=Mock(return_value=None), existing_proposal=Mock(return_value=None))
    registry = SimpleNamespace(recover_derived_thread=AsyncMock(), list_handles=Mock(return_value=[]),
        spawn_derived=AsyncMock(return_value=None), promote_derived=AsyncMock(), cancel=AsyncMock())
    compactor = AgentSummarizationCompactor(config_service=None, registry=registry, sessions=None,
        saver=SimpleNamespace(_storage=SimpleNamespace(compactions=candidates)))
    return owner, compactor, registry, candidates


async def test_no_historical_input_creates_no_task_or_candidate(compaction_runtime_case):
    owner, compactor, registry, candidates = compaction_runtime_case
    assert await compactor.request(owner, agent_id="coder", item_ids=()) is None
    registry.spawn_derived.assert_not_awaited()
    candidates.freeze.assert_not_called()


async def test_capacity_rejection_remains_explicit_and_never_freezes_candidate(compaction_runtime_case):
    owner, compactor, registry, candidates = compaction_runtime_case
    with pytest.raises(RuntimeError, match="compaction-capacity-exhausted"):
        await compactor.request(owner, agent_id="coder", item_ids=("historical-item",), model_recipe={"model": "first"})
    registry.spawn_derived.assert_awaited_once()
    candidates.freeze.assert_not_called()


@pytest.mark.parametrize("selected", [None, ModelReferenceDTO(provider_id="selected", model_id="model")])
async def test_manual_request_freezes_session_selected_model_before_acceptance(compaction_runtime_case, selected):
    from app.domain.context_compaction import CompactionPolicy

    owner, compactor, registry, candidates = compaction_runtime_case
    reference = selected or ModelReferenceDTO(provider_id="default", model_id="model")
    recipe = {"provider_id": reference.provider_id, "model_id": reference.model_id}
    snapshot = object()
    config = SimpleNamespace(
        get_snapshot=Mock(return_value=snapshot), use_snapshot=Mock(side_effect=nullcontext),
        resolve_agent_model=Mock(return_value=reference), freeze_model_recipe=Mock(return_value=recipe),
        get_compaction_policy=Mock(return_value=CompactionPolicy(target_fraction=0.45)),
    )
    compactor._config = config
    with pytest.raises(RuntimeError, match="compaction-capacity-exhausted"):
        await compactor.request(owner, agent_id="coder", preferred_model=selected, item_ids=("historical-item",), manual=True)

    config.use_snapshot.assert_called_once_with(snapshot)
    config.resolve_agent_model.assert_called_once_with("coder", selected)
    config.freeze_model_recipe.assert_called_once_with(reference)
    candidates.existing_proposal.assert_called_once()
    assert candidates.existing_proposal.call_args.kwargs["model_config_hash"] == sha256_jcs(recipe)
    assert candidates.existing_proposal.call_args.kwargs["target_constraint_hash"] is None
    config.get_compaction_policy.assert_called_once()
    registry.spawn_derived.assert_awaited_once()
    candidates.freeze.assert_not_called()


@pytest.mark.parametrize("foreground", [False, True])
async def test_active_request_reuses_original_task_and_only_foreground_promotes(compaction_runtime_case, foreground):
    owner, compactor, registry, candidates = compaction_runtime_case
    active = SimpleNamespace(task_id="existing", session_id=owner.session_id, thread_id=owner.thread_id,
        descriptor=SimpleNamespace(kind=CompactionTaskHandler.kind), status="running")
    registry.list_handles.return_value = [active]
    registry.promote_derived.return_value = active
    assert await compactor.request(owner, agent_id="coder", foreground=foreground) is active
    registry.spawn_derived.assert_not_awaited()
    candidates.freeze.assert_not_called()
    if foreground:
        registry.promote_derived.assert_awaited_once_with(owner.session_id, active.task_id)
    else:
        registry.promote_derived.assert_not_awaited()


async def test_cancelled_wait_cancels_only_its_original_task(compaction_runtime_case, monkeypatch):
    owner, compactor, registry, candidates = compaction_runtime_case
    active = SimpleNamespace(task_id="waited", session_id=owner.session_id, thread_id=owner.thread_id,
                             descriptor=object(), status="running",
                             metadata={"deadline": (datetime.now(UTC) + timedelta(seconds=10)).isoformat()})
    registry.promote_derived.return_value = active
    registry.get_handle = Mock(return_value=active)
    monkeypatch.setattr(CompactionTaskHandler, "descriptor_input", lambda _: (object(), "", datetime.now(UTC) + timedelta(seconds=10)))
    candidates.read_task_input = Mock(return_value=SimpleNamespace(frozen=SimpleNamespace(candidate_id="candidate")))
    entered = Event()

    def pending_result(*_args, **_kwargs):
        entered.set()

    candidates.read_result = pending_result
    task = asyncio.create_task(compactor.wait(active))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    registry.cancel.assert_awaited_once_with(owner.session_id, "waited")


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_observation_batching_changes_only_approximation_rounding(compaction_runtime_case, monkeypatch, protocol):
    from boxteam_plugin_sdk.lifecycle import LifetimeScope
    from langchain_core.messages import AIMessage

    from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
    from app.domain.context_compaction import (
        CompactionPolicy,
        ContextBudget,
        InputTokenEstimate,
        ModelCapacity,
    )
    from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
        LangChainMessageCodec,
    )

    owner, compactor, _registry, candidates = compaction_runtime_case
    policy = CompactionPolicy()
    monkeypatch.setattr(compactor, "policy", lambda: (policy, True))

    @asynccontextmanager
    async def admission(_session):
        yield

    compactor._sessions = SimpleNamespace(session_read_admission=admission)
    profile_cls = ChatWireProfile if protocol == "chat" else ResponsesWireProfile
    profile = profile_cls(provider_id="test", model="test", custom_llm_provider="openai")
    records = tuple(LangChainMessageCodec().items_for_message(
        AIMessage(content=f"稳定正文{index}:" + "abc " * 61, id=f"reply-{index}"),
        item_sequence=index + 1, message_id=f"reply-{index}", turn_id="turn", execution_id="execution",
        timestamp="2026-11-24T00:00:00Z",
    )[0] for index in range(12))
    budgets = []
    for grouped in (True, False):
        available = []

        def increment(_session, _thread, *, after_sequence, available=available):
            page = tuple(item for item in available if item.item_sequence > after_sequence)
            return (page[-1].item_sequence if page else after_sequence), page

        candidates.observation_increment = increment
        scope = LifetimeScope("batch-observation")
        compactor.bind_observation(owner, scope)
        compactor.effective_policy(owner, "generation", policy)
        compactor.seed_observation(owner, ContextBudget(capacity=ModelCapacity(max_input_tokens=100000),
            estimate=InputTokenEstimate(known_tokens=100, source="test-complete-wire")), 0, "generation",
            agent_id="default", model_recipe={}, wire_profile=profile)
        try:
            for batch in (records,) if grouped else ((item,) for item in records):
                previous = compactor._preparation_scopes[owner].observation_task
                available.extend(batch)
                compactor.observe_committed(owner)
                async with asyncio.timeout(5):
                    while compactor._preparation_scopes[owner].observation_task is previous:
                        await asyncio.sleep(0)
                    await compactor._preparation_scopes[owner].observation_task
            budgets.append(compactor._preparation_scopes[owner].observation_budget.estimate.known_tokens)
        finally:
            await scope.close()
    # 每次字符近似取整最多差一个token；不得重复累计每次请求的固定外框开销。
    assert abs(budgets[0] - budgets[1]) <= len(records)


@pytest.mark.parametrize("transition", ["generation", "close"])
async def test_observation_worker_cannot_write_after_scope_transition(compaction_runtime_case, monkeypatch, transition):
    from boxteam_plugin_sdk.lifecycle import LifetimeScope
    from langchain_core.messages import AIMessage

    from app.agents.providers.wire_finalize import ChatWireProfile
    from app.domain.context_compaction import (
        CompactionPolicy,
        ContextBudget,
        InputTokenEstimate,
        ModelCapacity,
    )
    from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
        LangChainMessageCodec,
    )

    owner, compactor, _registry, candidates = compaction_runtime_case
    policy = CompactionPolicy()
    monkeypatch.setattr(compactor, "policy", lambda: (policy, True))
    monkeypatch.setattr(compactor, "request", AsyncMock())

    @asynccontextmanager
    async def admission(_session):
        yield

    compactor._sessions = SimpleNamespace(session_read_admission=admission)
    scope = LifetimeScope("generation-observation")
    compactor.bind_observation(owner, scope)
    compactor.effective_policy(owner, "old", policy)
    baseline = ContextBudget(capacity=ModelCapacity(max_input_tokens=200),
                             estimate=InputTokenEstimate(known_tokens=10, source="test-complete-wire"))
    profile = ChatWireProfile(provider_id="test", model="test", custom_llm_provider="openai")
    compactor.seed_observation(owner, baseline, 0, "old", agent_id="default", model_recipe={}, wire_profile=profile)
    items = LangChainMessageCodec().items_for_message(AIMessage(content="超出准备阈值的稳定回复。" * 100, id="reply"),
        item_sequence=1, message_id="reply", turn_id="turn", execution_id="execution", timestamp="2026-11-24T00:00:00Z")
    entered, release = Event(), Event()

    def increment(_session, _thread, *, after_sequence):
        entered.set()
        assert release.wait(5)
        return (1, items) if after_sequence == 0 else (after_sequence, ())

    candidates.observation_increment = increment
    compactor.observe_committed(owner)
    close_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task = compactor._preparation_scopes[owner].observation_task
        if transition == "generation":
            compactor.effective_policy(owner, "new", policy)
            compactor.seed_observation(owner, baseline, 0, "new", agent_id="default", model_recipe={}, wire_profile=profile)
        else:
            close_task = asyncio.create_task(scope.close())
            await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(task, return_exceptions=True)
        assert results == [None] or transition == "close" and isinstance(results[0], asyncio.CancelledError)
        compactor.request.assert_not_awaited()
        if transition == "generation":
            assert compactor._preparation_scopes[owner].observation_budget is baseline
        else:
            await close_task
            assert owner not in compactor._preparation_scopes
    finally:
        release.set()
        await scope.close()
        if close_task is not None:
            await close_task


@pytest.mark.parametrize("policy,expected", [
    (CompactionPolicy(), [0.75, 0.70, 0.65, 0.60, 0.60]),
    (CompactionPolicy(target_fraction=0.73), [0.75, 0.75, 0.75, 0.75, 0.75]),
    (CompactionPolicy(prepare_fraction=0.58), [0.58] * 5),
])
async def test_wait_feedback_preserves_custom_thresholds_and_resets_only_its_scope(compaction_runtime_case, policy, expected):
    owner, compactor, _registry, _candidates = compaction_runtime_case
    sibling = MutationIntentOwner(owner.session_id, "sibling")
    assert compactor.effective_policy(owner, "generation", policy) == policy
    for index, fraction in enumerate(expected):
        task_id = f"background-{index}"
        compactor.note_background(owner, task_id)
        assert compactor.waiting_background(owner) is None
        compactor.observe_provider_wait(SimpleNamespace(session_id=owner.session_id, thread_id=owner.thread_id,
                                                       task_id=task_id), True)
        assert compactor.waiting_background(owner) == task_id
        # 另一模型代际或另一task的反馈不能修改本scope。
        compactor.record_generation_wait(owner, "previous-generation", task_id)
        compactor.record_generation_wait(owner, "generation", "unrelated")
        compactor.record_generation_wait(owner, "generation", task_id)
        compactor.record_generation_wait(owner, "generation", task_id)
        assert compactor.effective_policy(owner, "generation", policy).prepare_fraction == fraction
        assert compactor.effective_policy(sibling, "generation", policy) == policy
    assert compactor.effective_policy(owner, "changed-capacity", policy) == policy
    compactor.note_background(owner, "old-generation-task")
    compactor.record_generation_wait(owner, "generation", "old-generation-task")
    assert compactor.effective_policy(owner, "changed-capacity", policy) == policy
    await compactor.release_thread(owner.session_id, owner.thread_id, 1)
    assert compactor.effective_policy(owner, "changed-capacity", policy) == policy
