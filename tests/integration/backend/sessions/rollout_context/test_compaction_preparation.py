"""完整冻结投影在候选采用前后保持同一正文，查询不提前封存或消费来源。"""

from __future__ import annotations

import asyncio
import time

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.providers.wire_finalize import chat_wire_profile, chat_wire_params, responses_wire_profile, responses_wire_params
from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import ApplyCompactionCandidateIntent
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.rollout_context.checkpoint.seal.preparation import (
    PreparedProviderContext,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    compaction_runtime_case as compaction_runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    generation_case as generation_case,  # noqa: PLC0414 — 复用真实 SDK／Saver 生成装具。
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_derived_summary_request import (
    summary_model as summary_model,  # noqa: PLC0414
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
]


@pytest.mark.asyncio
async def test_simulated_candidate_matches_final_frozen_request_without_early_seal(
    generation_case, production_context, frozen_provider_wire, frozen_provider_activation,
):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    manager = production_context[4]
    candidate = freeze(count=1, payload_size=2400)
    summary = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 60),
    )
    accepted = saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="preparation-tail", acceptance_idempotency_key="preparation-tail-accept",
        job_id="preparation-tail-job", admission_idempotency_key="preparation-tail-admit",
        payload="保留本次新输入，旧规则可以被摘要。", payload_kind="text",
    )
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
        from app.domain.itemized.hashing import sha256_jcs

        selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
        config = build_checkpoint_config(thread)
        checkpoint = empty_checkpoint()
        config = saver.put(config, checkpoint, {"source": "input", "step": -1}, {})
        parameters = dict(
            turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key="prepare:before", seal_idempotency_key="seal:before",
            context_source_manager=manager, tool_set_supplier=lambda: selected,
            **frozen_provider_wire("production-provider", "chat_completions"),
            **frozen_provider_activation(saver, plan_creation_idempotency_key="prepare:before"),
        )
        before = connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()
        prepared = saver.freeze_context_for_provider(session, thread, **parameters)
        assert isinstance(prepared, PreparedProviderContext)
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == before
        assert prepared.simulate_candidate is not None
        simulated = prepared.simulate_candidate(candidate, summary)
        assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?",
                                  (candidate.candidate_id,)).fetchone() == (None,)
        assert controller.transport.hit_counts == {0: 1}
        adopted = await saver.aadopt_candidate(config, ApplyCompactionCandidateIntent(candidate, prepared.plan.active_view_id))
        parameters.update(request_messages=adopted.checkpoint["channel_values"]["messages"])
        actual = saver.freeze_context_for_provider(session, thread, **parameters)
        assert isinstance(actual, PreparedProviderContext)
        assert simulated.wire_request == actual.request.wire_request
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == before
        receipt = saver.seal_context_for_provider(actual)
        assert receipt["snapshot"].request_hash_preimage == simulated.wire_request
        assert "保留本次新输入" in str(receipt["snapshot"].request_hash_preimage)
        assert controller.transport.hit_counts == {0: 1}


@pytest.fixture
async def model_preparation_case(generation_case, compaction_runtime_case, production_context, request, monkeypatch):
    """真实模型准备、候选任务和 Saver；只隔离配置及 Session 生命周期。"""
    from contextlib import nullcontext
    from pathlib import Path

    from langchain.agents.middleware import ModelRequest
    from langgraph.runtime import ExecutionInfo, Runtime

    from app.agents.model_call_execution import ModelCallExecution
    from app.agents.model_call_graph import create_model_call_agent
    from app.agents.model_capability_routing import (
        _SELECTED_PROVIDER,
        ProviderModelCandidate,
    )
    from app.agents.policy import ToolPolicyResolver
    from app.agents.providers.request_budget import estimate_wire_input
    from app.agents.tool_set_producer import RegisteredToolSetProducer
    from app.domain.context_compaction import CompactionPolicy
    from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
    from app.services.infrastructure.tool_selection_store import ToolSelectionStore
    from app.services.orchestration.resource_activation.contracts import (
        reset_current_execution_resource_activation,
        set_current_execution_resource_activation,
    )
    from tests.harness.python.run_context import TestRunContext

    saver, session, thread, connection, model, controller, activation, _freeze = generation_case
    compactor, registry, handler, recipe, _preparations = compaction_runtime_case
    handler._provider_wait = compactor.observe_provider_wait
    workload = getattr(request, "param", None)
    media_mode = workload if workload in {"old-media", "new-media"} else None
    if media_mode:
        model.image_input_replay = True
    # 先接受旧内容再接受新目标；摘要只能替换最新用户输入以前的闭合范围。
    history_size = 120000 if workload == "large-window" else 1200
    current_goal = "保留新的最终目标" * (2000 if workload == "large-latest" else 1)
    for label, text in (("large-history", "需要保留的旧约束。" * history_size), ("current-goal", current_goal)):
        payload = ([{"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]
                   if media_mode and (label == "large-history" or media_mode == "new-media") else text)
        accepted = saver.accept_turn(
            session, thread_id=thread, accepted_ingress_id=label, acceptance_idempotency_key=f"accept:{label}",
            job_id=f"job:{label}", admission_idempotency_key=f"admit:{label}", payload=payload,
            payload_kind="text" if isinstance(payload, str) else "structured_content",
        )
        if workload == "typed-scope-performance" and label == "large-history":
            historical_admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
            carrier = CanonicalBlockAccumulator(turn_id=historical_admission.turn_id,
                execution_id=historical_admission.execution_id, producer_id="performance-active-model",
                model_call_id="performance-active-model")
            carrier.accept({"block_id": "performance-active-model:block:part-0", "block_index": 0,
                            "carrier_type": "text", "text": "保留真实历史回复及其边界。"})
            with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
                  execution_admission_scope(historical_admission, langgraph_thread_id=thread)):
                next_sequence = connection.execute("SELECT MAX(item_sequence)+1 FROM item_catalog").fetchone()[0]
                saver.append_items(session, carrier.finalize(first_item_sequence=next_sequence), thread_id=thread)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
    policy = CompactionPolicy()
    enabled = [False]
    monkeypatch.setattr(compactor, "policy", lambda: (policy, enabled[0]))
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    producer = RegisteredToolSetProducer(
        agent_id="default", selection_reader=ToolSelectionStore(boxteam_root=context.workspace_root / ".boxteam"),
        policy_resolver=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={}), tools=(), metadata={},
    )
    execution = ModelCallExecution(
        checkpointer=saver, session_id=session, thread_id=thread,
        context_source_manager=production_context[4], tool_set_producer=producer,
        resource_activation=activation, context_read_admission=nullcontext,
        context_control_admission=lambda *_args: nullcontext(), compaction_runtime=compactor,
        agent_id="default", compaction_model_recipe=lambda _model: recipe,
    )
    create_model_call_agent(model, execution=execution, tools=[])
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        config = saver.put(build_checkpoint_config(thread), empty_checkpoint(), {"source": "input", "step": -1}, {})
        parent = await activation.freeze_execution(
            admission=admission, policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}),
        )
        token = set_current_execution_resource_activation(parent, admission=admission)
        selected = _SELECTED_PROVIDER.set(ProviderModelCandidate(
            "summary", "summary", model, frozenset({"text_input", "image_input"} if media_mode else {"text_input"}),
        ))
        try:
            def incoming(task):
                return ModelRequest(model=model, messages=[], state={"messages": []}, tools=[], runtime=Runtime(
                    execution_info=ExecutionInfo(thread_id=thread, checkpoint_ns="", checkpoint_id=config["configurable"]["checkpoint_id"],
                                                 task_id=task, node_attempt=1),
                ))
            model.profile = {**(model.profile or {}), "max_input_tokens": 1000000, "context_window": None}
            baseline = await execution._aprepare(incoming("baseline"))
            tokens = estimate_wire_input(baseline["snapshot"].request_hash_preimage).known_tokens
            assert tokens is not None and tokens > 1000
            assert not controller.installed
            yield execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread
        finally:
            _SELECTED_PROVIDER.reset(selected)
            reset_current_execution_resource_activation(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_manual_ready_candidate_is_adopted_below_auto_threshold(model_preparation_case, automatic):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.agents.manual_compaction_checkpoint import (
        ManualCompactionCheckpointAdapter,
    )
    from app.services.business.context_compaction_service import (
        ContextCompactionService,
    )

    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread = model_preparation_case
    enabled[0] = automatic
    model.profile = {**model.profile, "max_input_tokens": tokens * 10}
    owner = MutationIntentOwner(session, thread)

    @asynccontextmanager
    async def admission(_session_id):
        yield

    async def get_session(_session_id):
        return SimpleNamespace(current_agent_id="default", current_model=ModelReferenceDTO(provider_id="summary", model_id="summary"))

    async def resolve_thread(_session_id, requested):
        assert requested == thread
        return thread

    async def idle_operation(_session_id, _thread_id, operation):
        return await operation()

    service = ContextCompactionService(
        checkpoint_adapter=ManualCompactionCheckpointAdapter(checkpointer=execution._checkpointer),
        job_service=SimpleNamespace(run_thread_idle_operation=idle_operation),
        session_service=SimpleNamespace(session_write_admission=admission, get=get_session, resolve_thread=resolve_thread),
        summarization_compactor=compactor,
    )
    scheduled = await service.request_compaction(session, thread_id=thread)
    assert scheduled.status == "scheduled"
    handle = registry.get_handle(session, scheduled.task_id)
    frozen, result = await compactor.wait(handle)
    manifest = connection.execute("SELECT manifest_json FROM context_compaction_candidates WHERE task_id=?", (handle.task_id,)).fetchone()
    await compactor.request_application(handle)
    assert connection.execute("SELECT manifest_json FROM context_compaction_candidates WHERE task_id=?", (handle.task_id,)).fetchone() == manifest
    assert connection.execute("SELECT apply_on_next_request,applied_view_id FROM context_compaction_candidates WHERE task_id=?",
                              (handle.task_id,)).fetchone() == (1, None)
    # 重新构造的读取端只依赖原持久候选，不依赖运行端口里的临时状态。
    from app.services.infrastructure.rollout_context.checkpoint.durable.compaction_candidates import (
        RolloutCompactionCandidates,
    )

    cold = RolloutCompactionCandidates(execution._checkpointer._storage)
    ready = cold.latest_ready(session, thread, include_automatic=False)
    assert ready.frozen == frozen and ready.result == result and ready.apply_on_next_request
    with execution.graph_request_scope() as adopted:
        prepared = await execution._aprepare(incoming("manual-next-request"))
        assert adopted() is not None
    wire = str(prepared["snapshot"].request_hash_preimage)
    assert "保留新的最终目标" in wire
    assert "需要保留的旧约束。" * 10 not in wire
    assert compactor.ready(owner, include_automatic=False) is None
    assert [entry.task_id for entry in registry.list_summaries(session, include_history=True)] == [handle.task_id]
    assert controller.transport.hit_counts == {0: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_timing", ["before-freeze", "during-generation"])
async def test_background_successor_inherits_unconsumed_manual_intent_before_next_request(
    model_preparation_case, compaction_runtime_case, monkeypatch, manual_timing,
):
    from app.domain.itemized.hashing import sha256_jcs

    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread = model_preparation_case
    saver = execution._checkpointer
    candidates = saver._storage.compactions
    recipe = compaction_runtime_case[3]
    owner = MutationIntentOwner(session, thread)
    first = candidates.freeze(session, thread, candidate_id="manual-prefix", task_id="manual-prefix-task",
        item_ids=tuple(item.item_id for item in saver.read_canonical_items(session, thread)[:2]), model_config_hash=sha256_jcs(recipe))
    await CompactionGenerator(saver=saver, model=model, activation=execution._resource_activation).generate(
        first, budget=SummaryCallBudget(time.time() + 60),
    )
    assert compactor.ready(owner, include_automatic=False) is None
    if manual_timing == "before-freeze":
        candidates.request_application(session, thread, first.task_id)
    entered, release = asyncio.Event(), asyncio.Event()
    dispatch = controller.transport.handle_async_request

    async def hold_successor(request, **kwargs):
        entered.set()
        await release.wait()
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", hold_successor)
    handle = await compactor.request(owner, agent_id="default", model_recipe=recipe)
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        if manual_timing == "during-generation":
            assert compactor.ready(owner, include_automatic=False) is None
            candidates.request_application(session, thread, first.task_id)
        assert connection.execute("SELECT apply_on_next_request FROM context_compaction_candidates WHERE task_id=?",
                                  (handle.task_id,)).fetchone() == (0,)
        release.set()
        frozen, result = await compactor.wait(handle)
    finally:
        release.set()
    assert frozen.predecessor.candidate_id == first.candidate_id
    ready = compactor.ready(owner, include_automatic=False)
    assert ready.frozen == frozen and ready.result == result and ready.apply_on_next_request
    enabled[0] = False
    model.profile = {**model.profile, "max_input_tokens": tokens * 10}
    with execution.graph_request_scope() as adopted:
        prepared = await execution._aprepare(incoming("manual-successor-next-request"))
        assert adopted() is not None
    wire = str(prepared["snapshot"].request_hash_preimage)
    assert "保留新的最终目标" in wire and "需要保留的旧约束。" * 10 not in wire
    assert connection.execute("SELECT candidate_id FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchall() == [(frozen.candidate_id,)]
    assert connection.execute("SELECT discarded_reason FROM context_compaction_candidates WHERE candidate_id=?",
                              (first.candidate_id,)).fetchone() == ("superseded-by-successor",)
    assert compactor.ready(owner, include_automatic=False) is None
    assert len(registry.list_summaries(session, include_history=True)) == 1
    assert controller.transport.hit_counts == {0: 2}


@pytest.mark.asyncio
async def test_model_preparation_starts_background_then_adopts_ready_without_second_summary(model_preparation_case, monkeypatch):
    import math

    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )
    from app.services.orchestration.background_work.compaction import (
        CompactionTaskHandler,
    )

    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, _thread = model_preparation_case
    saver = execution._checkpointer
    freeze = saver.freeze_context_for_provider
    preparations = []

    def record_frozen(*args, **kwargs):
        prepared = freeze(*args, **kwargs)
        if isinstance(prepared, PreparedProviderContext) and prepared.project_retained_input is not None:
            preparations.append(prepared)
        return prepared

    monkeypatch.setattr(saver, "freeze_context_for_provider", record_frozen)
    enabled[0] = True
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.85)}
    with execution.graph_request_scope() as adopted:
        ready_to_send = await execution._aprepare(incoming("prepare-threshold"))
        assert adopted() is None
    assert "保留新的最终目标" in str(ready_to_send["snapshot"].request_hash_preimage)
    handles = registry.list_handles(session)
    assert len(handles) == 1
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE task_id=?", (handles[0].task_id,)).fetchone() == (None,)
    async with asyncio.timeout(30):
        frozen, summary = await compactor.wait(handles[0])
    assert len(preparations) == 1
    prepared = preparations[0]
    retained = prepared.project_retained_input().budget(prepared.capacity)
    assert retained.estimate.input_tokens is not None and retained.estimate.input_tokens > 0
    ref, namespace, _deadline = CompactionTaskHandler.descriptor_input(handles[0].descriptor)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        task_input = cold._storage.compactions.read_task_input(session, _thread, ref, checkpoint_ns=namespace)
        instruction = str(cold.read_context_for_dispatch(
            summary.dispatch_ref,
            wire_profile=responses_wire_profile(model) if summary.dispatch_ref.target_format == "responses" else chat_wire_profile(model),
            wire_params=responses_wire_params(model, kwargs={}) if summary.dispatch_ref.target_format == "responses" else chat_wire_params(model, stop=None, kwargs={}, stream=True),
        )["snapshot"].request_hash_preimage["messages"][-1]["content"])
    target = task_input.target_constraint
    assert target["capacity"] == prepared.capacity.model_dump(mode="json")
    assert target["retained_input_tokens"] == retained.estimate.input_tokens
    allowance = math.floor(prepared.capacity.input_budget_tokens * target["policy"]["target_fraction"]) - retained.estimate.input_tokens
    assert "55%" in instruction and f"soft allowance of {allowance} input tokens" in instruction
    assert controller.transport.hit_counts == {0: 1}
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.95)}
    with execution.graph_request_scope() as adopted:
        compacted = await execution._aprepare(incoming("apply-threshold"))
        assert adopted() is not None
    assert "保留新的最终目标" in str(compacted["snapshot"].request_hash_preimage)
    assert "需要保留的旧约束。" * 10 not in str(compacted["snapshot"].request_hash_preimage)
    assert controller.transport.hit_counts == {0: 1}
    assert registry.get_handle(session, handles[0].task_id).status == "completed"
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (frozen.candidate_id,)).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fraction,allowed", [(0.95, True), (1.10, False)])
async def test_model_preparation_auto_disabled_preserves_capacity_boundary(model_preparation_case, fraction, allowed):
    from app.domain.context_compaction import ContextCapacityError

    execution, incoming, model, tokens, enabled, _compactor, registry, connection, controller, session, _thread = model_preparation_case
    enabled[0] = False
    model.profile = {**model.profile, "max_input_tokens": int(tokens / fraction)}
    before = connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()
    with execution.graph_request_scope() as adopted:
        if allowed:
            result = await execution._aprepare(incoming("disabled"))
            assert "保留新的最终目标" in str(result["snapshot"].request_hash_preimage)
        else:
            with pytest.raises(ContextCapacityError):
                await execution._aprepare(incoming("disabled-overflow"))
            assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == before
        assert adopted() is None
    assert registry.list_handles(session) == []
    assert not controller.installed


@pytest.mark.asyncio
async def test_actual_background_model_wait_lowers_next_prepare_fraction_once(model_preparation_case, monkeypatch):
    from datetime import UTC, datetime

    from langchain_core.messages import AIMessage

    from app.agents.model_capability_routing import current_selected_provider
    from app.domain.context_compaction import CompactionPolicy
    from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
        LangChainMessageCodec,
    )

    execution, incoming, model, tokens, enabled, compactor, registry, _connection, controller, session, thread = model_preparation_case
    enabled[0] = True
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.85)}
    # 安装现有回放transport，在真实SDK网络边界冻结结果，不替产品生成摘要。
    controller.ensure_installed()
    transport = controller.transport
    original = transport.handle_async_request
    entered, release, foreground_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held_request(request):
        entered.set()
        await release.wait()
        return await original(request)

    monkeypatch.setattr(transport, "handle_async_request", held_request)
    original_wait = compactor.wait

    async def observed_wait(handle, **kwargs):
        foreground_waiting.set()
        return await original_wait(handle, **kwargs)

    monkeypatch.setattr(compactor, "wait", observed_wait)
    owner = MutationIntentOwner(session, thread)
    try:
        with execution.graph_request_scope():
            await execution._aprepare(incoming("background-before-reply"))
        async with asyncio.timeout(10):
            await entered.wait()
        handles = registry.list_handles(session)
        assert len(handles) == 1
        assert compactor.waiting_background(owner) == handles[0].task_id
        # 稳定assistant增量推动阈值；容量、模型和策略代际均保持不变。
        admission = execution._request_preparation(incoming("append-reply")).admission
        items = LangChainMessageCodec().items_for_message(
            AIMessage(content="工具循环结束后的详细回复。" * 200, id="stable-assistant-growth"),
            item_sequence=1, message_id="stable-assistant-growth", turn_id=admission.turn_id,
            execution_id=admission.execution_id, timestamp=datetime.now(UTC).isoformat(),
        )
        execution._checkpointer.append_items(session, items, thread_id=thread)
        with execution.graph_request_scope() as adopted:
            task = asyncio.create_task(execution._aprepare(incoming("foreground-after-reply")))
            async with asyncio.timeout(10):
                await foreground_waiting.wait()
            assert not task.done()
            release.set()
            async with asyncio.timeout(30):
                result = await task
            assert adopted() is not None
        assert "工具循环结束后的详细回复。" in str(result["snapshot"].request_hash_preimage)
        status = compactor.status(owner)
        assert status["policy"]["prepare_fraction"] == 0.80
        assert status["effective_prepare_fraction"] == 0.75
        assert status["feedback_task_id"] == handles[0].task_id
        assert controller.transport.hit_counts == {0: 1}
        # 同task重复反馈不再下调；卸载原scope恢复配置基准。
        generation = compactor._preparation_scopes[owner].generation
        compactor.record_generation_wait(owner, generation, handles[0].task_id)
        assert compactor.status(owner)["effective_prepare_fraction"] == 0.75
        assert current_selected_provider() is model
        await compactor.release_thread(session, thread, 1)
        assert compactor.effective_policy(owner, generation, CompactionPolicy()).prepare_fraction == 0.80
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_preparation_case", ["old-media", "new-media"], indirect=True)
async def test_ready_old_media_summary_allows_text_model_but_new_media_still_requires_vision(model_preparation_case, monkeypatch):
    from app.agents.model_capability_routing import (
        _SELECTED_PROVIDER,
        ProviderCapabilityError,
        ProviderModelCandidate,
    )
    from app.domain.context_compaction import CompactionPolicy
    from app.domain.itemized.hashing import sha256_jcs

    execution, incoming, model, _tokens, enabled, compactor, _registry, connection, controller, session, thread = model_preparation_case
    saver = execution._checkpointer
    items = saver.read_canonical_items(session, thread)
    candidate = saver._storage.compactions.freeze(
        session, thread, candidate_id="media-summary", task_id="media-summary-task",
        item_ids=[item.item_id for item in items[:-1]], model_config_hash=sha256_jcs({"model": "vision"}),
    )
    result = await CompactionGenerator(saver=saver, model=model, activation=execution._resource_activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 60),
    )
    assert result.coverage == candidate.refs
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    enabled[0] = True
    monkeypatch.setattr(compactor, "policy", lambda: (CompactionPolicy(apply_policy="next_request"), True))
    selected = _SELECTED_PROVIDER.set(ProviderModelCandidate("summary", "summary", model, frozenset({"text_input"})))
    try:
        with execution.graph_request_scope() as adopted:
            if isinstance(items[-1].payload, list):
                with pytest.raises(ProviderCapabilityError, match="image_input"):
                    await execution._aprepare(incoming("new-media-rejected"))
                assert adopted() is None
                assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
                assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?",
                                          (candidate.candidate_id,)).fetchone() == (None,)
            else:
                prepared = await execution._aprepare(incoming("old-media-replaced"))
                assert adopted() is not None
                wire = prepared["snapshot"].request_hash_preimage
                assert "image_url" not in str(wire)
                assert "保留新的最终目标" in str(wire)
        assert controller.transport.hit_counts == {0: 1}
    finally:
        _SELECTED_PROVIDER.reset(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_preparation_case", ["new-media"], indirect=True)
async def test_sealed_retry_rechecks_actual_model_capabilities_without_resealing(model_preparation_case):
    from app.agents.model_capability_routing import (
        _SELECTED_PROVIDER,
        ProviderCapabilityError,
        ProviderModelCandidate,
    )

    execution, incoming, model, _tokens, _enabled, _compactor, _registry, connection, controller, _session, _thread = model_preparation_case
    with execution.graph_request_scope():
        first = await execution._aprepare(incoming("sealed-media-retry"))
    assert "image_url" in str(first["snapshot"].request_hash_preimage)
    before = connection.execute("SELECT assembly_id,request_hash FROM context_assemblies ORDER BY rowid").fetchall()
    selected = _SELECTED_PROVIDER.set(ProviderModelCandidate("summary", "summary", model, frozenset({"text_input"})))
    try:
        with execution.graph_request_scope(), pytest.raises(ProviderCapabilityError, match="image_input"):
            await execution._aprepare(incoming("sealed-media-retry"))
        assert connection.execute("SELECT assembly_id,request_hash FROM context_assemblies ORDER BY rowid").fetchall() == before
        assert not controller.installed
    finally:
        _SELECTED_PROVIDER.reset(selected)


@pytest.mark.asyncio
async def test_insufficient_ready_candidate_is_only_simulated_before_one_complete_successor_adoption(model_preparation_case):
    from app.domain.itemized.hashing import sha256_jcs

    execution, incoming, model, tokens, enabled, _compactor, _registry, connection, controller, session, thread = model_preparation_case
    saver = execution._checkpointer
    items = saver.read_canonical_items(session, thread)
    first = saver._storage.compactions.freeze(
        session, thread, candidate_id="insufficient-prefix", task_id="insufficient-prefix-task",
        # 第一条装具消息很短；连同下一条真实历史生成确有缩减、但仍不足最终窗口的摘要。
        item_ids=tuple(item.item_id for item in items[:2]), model_config_hash=sha256_jcs({"model": "original-large-window"}),
    )
    await CompactionGenerator(saver=saver, model=model, activation=execution._resource_activation).generate(
        first, budget=SummaryCallBudget(time.time() + 60),
    )
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    enabled[0] = True
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.95)}
    with execution.graph_request_scope() as adopted:
        result = await execution._aprepare(incoming("complete-successor"))
        assert adopted() is not None
    assert "保留新的最终目标" in str(result["snapshot"].request_hash_preimage)
    assert "需要保留的旧约束。" * 10 not in str(result["snapshot"].request_hash_preimage)
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?",
                              (first.candidate_id,)).fetchone() == (None,)
    applied = connection.execute(
        "SELECT json_extract(manifest_json,'$.predecessor.candidate_id'),applied_view_id "
        "FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL",
    ).fetchall()
    assert len(applied) == 1 and applied[0][0] == first.candidate_id
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() != before
    assert controller.transport.hit_counts == {0: 2}


@pytest.mark.asyncio
async def test_inflight_old_prefix_gets_one_successor_with_shared_deadline_and_one_adoption(model_preparation_case, monkeypatch):
    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread = model_preparation_case
    saver = execution._checkpointer
    owner = MutationIntentOwner(session, thread)
    items = saver.read_canonical_items(session, thread)
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    entered, release, foreground_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, foreground_requests, waits, adoptions = [], [], [], []
    original_request, original_wait, original_adopt = compactor.request, compactor.wait, saver.aadopt_candidate

    async def held_request(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            await release.wait()
        return await dispatch(request, **kwargs)

    async def observed_request(request_owner, **kwargs):
        if kwargs.get("foreground"):
            foreground_requests.append(kwargs)
        return await original_request(request_owner, **kwargs)

    async def observed_wait(handle, **kwargs):
        waits.append((handle.task_id, kwargs["deadline"]))
        foreground_waiting.set()
        return await original_wait(handle, **kwargs)

    async def observed_adopt(config, intent):
        adoptions.append(intent.frozen.candidate_id)
        return await original_adopt(config, intent)

    monkeypatch.setattr(controller.transport, "handle_async_request", held_request)
    monkeypatch.setattr(compactor, "request", observed_request)
    monkeypatch.setattr(compactor, "wait", observed_wait)
    monkeypatch.setattr(saver, "aadopt_candidate", observed_adopt)
    task = None
    try:
        first = await compactor.request(
            owner, agent_id="default", item_ids=tuple(item.item_id for item in items[:2]),
            model_recipe=execution._compaction_model_recipe(model),
        )
        async with asyncio.timeout(10):
            await entered.wait()
        assert registry.get_handle(session, first.task_id).status == "running"
        before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
        enabled[0] = True
        model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.95)}
        with execution.graph_request_scope() as adopted:
            task = asyncio.create_task(execution._aprepare(incoming("inflight-successor")))
            async with asyncio.timeout(10):
                await foreground_waiting.wait()
            assert not task.done() and waits[0][0] == first.task_id
            assert len(requests) == 1 and adoptions == []
            release.set()
            async with asyncio.timeout(30):
                result = await task
            assert adopted() is not None
        assert len(foreground_requests) == len(waits) == 2
        assert foreground_requests[0]["deadline"] == foreground_requests[1]["deadline"] == waits[0][1] == waits[1][1]
        handles = [registry.get_handle(session, summary.task_id) for summary in registry.list_summaries(session, include_history=True)]
        assert len(handles) == 2 and len(requests) == 2 and controller.transport.hit_counts == {0: 2}
        second = next(handle for handle in handles if handle.task_id != first.task_id)
        assert waits[1][0] == second.task_id
        assert second.descriptor.payload["deadline"] == waits[0][1].isoformat()
        assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE task_id=?", (first.task_id,)).fetchone() == (None,)
        applied = connection.execute(
            "SELECT candidate_id,json_extract(manifest_json,'$.predecessor.candidate_id') "
            "FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL",
        ).fetchall()
        assert adoptions == [second.task_id]
        assert applied == [(second.task_id, first.task_id)]
        assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() != before
        assert "保留新的最终目标" in str(result["snapshot"].request_hash_preimage)
        assert "需要保留的旧约束。" * 10 not in str(result["snapshot"].request_hash_preimage)
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [True, False])
@pytest.mark.parametrize("model_preparation_case", ["large-window"], indirect=True)
async def test_real_400k_to_200k_routing_reprepares_and_compacts_only_for_actual_fallback(model_preparation_case, monkeypatch, asynchronous):
    import json

    import httpx

    from app.agents.model_capability_routing import (
        CapabilityRoutingMiddleware,
        ProviderModelCandidate,
    )
    execution, incoming, model, tokens, enabled, compactor, _registry, connection, controller, session, thread = model_preparation_case
    assert 200000 < tokens < 320000
    model.profile = {**model.profile, "max_input_tokens": 400000}
    model = model.model_copy(update={"invocation_purpose": "agent"})
    fallback = model.model_copy(update={"provider_id": "smaller-fallback", "profile": {**model.profile, "max_input_tokens": 200000}})
    routing = CapabilityRoutingMiddleware((
        ProviderModelCandidate("summary", "large", model, frozenset({"text_input"})),
        ProviderModelCandidate("smaller-fallback", "small", fallback, frozenset({"text_input"})),
    ))
    enabled[0] = True
    receipts, events, requests = [], [], []

    async def report(name, payload):
        events.append((name, payload))

    monkeypatch.setattr("app.agents.model_capability_routing.adispatch_custom_event", report)
    monkeypatch.setattr("app.agents.model_capability_routing.dispatch_custom_event", lambda name, payload: events.append((name, payload)))
    project = execution._project_prepared

    def record_projection(request, prepared):
        receipts.append(prepared["snapshot"])
        return project(request, prepared)

    monkeypatch.setattr(execution, "_project_prepared", record_projection)
    controller.ensure_installed()
    dispatch, dispatch_sync = controller.transport.handle_async_request, controller.transport.handle_request

    def first_failure(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchone() == (0,)
            return httpx.Response(401, json={"error": {"message": "首选 Provider 拒绝本次凭据", "type": "authentication_error"}}, request=request)

    async def fail_then_replay(request, **kwargs):
        if (failure := first_failure(request)) is not None:
            return failure
        return await dispatch(request, **kwargs)

    def fail_then_replay_sync(request, **kwargs):
        if (failure := first_failure(request)) is not None:
            return failure
        return dispatch_sync(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", fail_then_replay)
    monkeypatch.setattr(controller.transport, "handle_request", fail_then_replay_sync)

    with execution.graph_request_scope() as adopted:
        result = (await routing.awrap_model_call(incoming("actual-fallback"), execution.aexecute) if asynchronous else
                  await asyncio.to_thread(routing.wrap_model_call, incoming("actual-fallback"), execution.execute))
        assert adopted() is not None, {"requests": len(requests), "events": events, "receipts": len(receipts), "response": result}
    assert result.result[0].text
    assert len(receipts) == 2 and receipts[0].provider_version != receipts[1].provider_version
    assert receipts[0].request_hash != receipts[1].request_hash
    assert "需要保留的旧约束。" * 10 in str(receipts[0].request_hash_preimage)
    assert "需要保留的旧约束。" * 10 not in str(receipts[1].request_hash_preimage)
    assert "保留新的最终目标" in str(receipts[1].request_hash_preimage)
    assert len(requests) == 3
    assert receipts[0].request_hash_preimage["messages"] == requests[0]["messages"]
    assert receipts[1].request_hash_preimage["messages"] == requests[-1]["messages"]
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchone() == (1,)
    assert len(events) == 1 and events[0][1]["provider_id"] == "summary"
    assert controller.transport.hit_counts == {0: 2}
    assert compactor.status(MutationIntentOwner(session, thread))["effective_prepare_fraction"] == 0.80


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [True, False])
@pytest.mark.parametrize("automatic,repeated_overflow", [(True, False), (True, True), (False, False)])
async def test_unknown_capacity_sdk_overflow_compacts_once_and_preserves_failed_request(
    model_preparation_case, monkeypatch, automatic, repeated_overflow, asynchronous,
):
    import json

    import httpx

    from app.runtime.litellm_runtime import litellm
    from app.runtime.provider_errors import is_context_overflow

    execution, incoming, model, _tokens, enabled, compactor, _registry, connection, controller, session, thread = model_preparation_case
    enabled[0] = automatic
    model.profile = {**model.profile, "max_input_tokens": None, "context_window": None}
    model = model.model_copy(update={"invocation_purpose": "agent"})
    request = incoming("unknown-overflow").override(model=model)
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    dispatch_sync = controller.transport.handle_request
    requests = []

    def overflow_response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1 or repeated_overflow and len(requests) == 3:
            return httpx.Response(400, json={"error": {
                "message": "maximum context length exceeded", "type": "invalid_request_error",
                "code": "context_length_exceeded", "param": "messages",
            }}, request=request)

    async def overflow_then_replay(request, **kwargs):
        if (response := overflow_response(request)) is not None:
            return response
        return await dispatch(request, **kwargs)

    def overflow_then_replay_sync(request, **kwargs):
        if (response := overflow_response(request)) is not None:
            return response
        return dispatch_sync(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", overflow_then_replay)
    monkeypatch.setattr(controller.transport, "handle_request", overflow_then_replay_sync)
    original_count = connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0]
    with execution.graph_request_scope() as adopted:
        if automatic and not repeated_overflow:
            response = await execution.aexecute(request) if asynchronous else await asyncio.to_thread(execution.execute, request)
            assert response.result[0].text
        else:
            with pytest.raises(litellm.BadRequestError) as failure:
                if asynchronous:
                    await execution.aexecute(request)
                else:
                    await asyncio.to_thread(execution.execute, request)
            assert is_context_overflow(failure.value)
        assert (adopted() is not None) is automatic
    # 真实 SDK 首次失败、摘要及重发共三次；再次 overflow 不再产生摘要或第四次发送。
    assert len(requests) == (3 if automatic else 1)
    assert controller.transport.hit_counts == ({0: 1 if repeated_overflow else 2} if automatic else {})
    applied = connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchone()[0]
    assert applied == int(automatic)
    snapshots = connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall()[original_count:]
    assert len(snapshots) == (3 if automatic else 1)
    first = execution._checkpointer._storage.assemblies.read_context_assembly(
        session, thread, assembly_id=snapshots[0][0], checkpoint_ns="",
    )[0]
    assert first.request_hash_preimage["messages"] == requests[0]["messages"]
    if automatic:
        final = execution._checkpointer._storage.assemblies.read_context_assembly(
            session, thread, assembly_id=snapshots[-1][0], checkpoint_ns="",
        )[0]
        assert first.request_hash != final.request_hash
        assert final.request_hash_preimage["messages"] == requests[-1]["messages"]
        assert "保留新的最终目标" in str(requests[-1])
        assert "需要保留的旧约束。" * 10 not in str(requests[-1])
        # 没有可信容量，缩减不能伪装成已知的预算适配。
        assert model.profile["max_input_tokens"] is None and model.profile["context_window"] is None
        assert compactor.status(MutationIntentOwner(session, thread))["effective_prepare_fraction"] == 0.80


@pytest.mark.asyncio
@pytest.mark.parametrize("model_preparation_case", ["large-latest"], indirect=True)
async def test_all_models_reject_irreducible_latest_input_without_adopting_insufficient_candidates(
    model_preparation_case, monkeypatch,
):
    from app.agents.model_capability_routing import (
        CapabilityRoutingMiddleware,
        ProviderModelCandidate,
    )
    from app.domain.context_compaction import ContextCapacityError

    execution, incoming, model, _tokens, enabled, compactor, _registry, connection, controller, session, thread = model_preparation_case
    enabled[0] = True
    first = model.model_copy(update={"invocation_purpose": "agent", "profile": {**model.profile, "max_input_tokens": 1800}})
    second = first.model_copy(update={"provider_id": "smaller", "profile": {**model.profile, "max_input_tokens": 1200}})
    routing = CapabilityRoutingMiddleware((
        ProviderModelCandidate("summary", "first", first, frozenset({"text_input"})),
        ProviderModelCandidate("smaller", "second", second, frozenset({"text_input"})),
    ))
    events, main_sends = [], []

    async def report(*event):
        events.append(event)

    project = execution._project_prepared

    def record(request, prepared):
        main_sends.append(prepared["snapshot"])
        return project(request, prepared)

    monkeypatch.setattr("app.agents.model_capability_routing.adispatch_custom_event", report)
    monkeypatch.setattr(execution, "_project_prepared", record)
    before = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall()
    candidates_before = connection.execute("SELECT COUNT(*) FROM context_compaction_candidates").fetchone()
    with execution.graph_request_scope() as adopted:
        with pytest.raises(ContextCapacityError) as failure:
            await routing.awrap_model_call(incoming("irreducible-latest"), execution.aexecute)
        assert adopted() is None
    assert failure.value.budget.exceeds_capacity
    assert not events and not main_sends
    assert connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchall() == before
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchone() == (0,)
    assert "保留新的最终目标" * 2000 in str(execution._checkpointer.read_canonical_items(session, thread)[-1].payload)
    assert not controller.installed
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates").fetchone() == candidates_before
    assert compactor.status(MutationIntentOwner(session, thread))["effective_prepare_fraction"] == 0.80
