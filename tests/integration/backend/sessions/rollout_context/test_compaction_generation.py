"""真实 Saver、派生 execution 与 SDK 回放验证完整分块生成，不替产品预制结果。"""

from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import messages_from_dict
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.agents.summarization.state import SummaryGenerationError
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_catalog_components
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.context_compaction import CompactionPolicy, ContextCapacityError
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
    ReadyCompactionCandidate,
    compaction_selection_ids,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.context_compaction import AgentSummarizationCompactor
from app.runtime.network_proxy import ProxyRoutes
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.business.message_service import MessageService
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from app.services.infrastructure.session_trace.store import TraceEventStore
from app.services.orchestration.background_work.compaction import CompactionTaskHandler
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from configs.network import DirectProxy
from tests.harness.python.run_context import TestRunContext
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


@pytest.fixture
def generation_case(production_context, summary_model):
    saver, session, thread, connection, _manager, prepare = production_context
    prepare("generation-parent")
    model, controller, _protocol = summary_model
    activation = ResourceActivationCoordinator(
        registry=ResourceRegistry(), saver=saver, body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
    )

    def freeze(*, count=3, payload_size=2400):
        for index in range(count):
            saver.accept_turn(
                session, thread_id=thread, accepted_ingress_id=f"input-{index}", acceptance_idempotency_key=f"accept-{index}",
                job_id=f"job-{index}", admission_idempotency_key=f"admit-{index}",
                payload=f"原始约束{index}:" + "abc " * (payload_size // 4), payload_kind="text",
            )
        items = saver.read_canonical_items(session, thread)
        candidate = saver._storage.compactions.freeze(
            session, thread, candidate_id="full-generation", task_id="generation-task",
            item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({"model": "frozen"}),
        )
        return candidate

    return saver, session, thread, connection, model, controller, activation, freeze


@pytest.fixture
async def compaction_runtime_case(generation_case, request):
    saver, session, thread, _connection, _model, _controller, activation, freeze = generation_case
    freeze(count=1, payload_size=400)
    # runtime 自己冻结范围，去掉仅供生成器测试的未受理候选。
    saver._storage.compactions.discard(session, thread, "full-generation", reason="generator-fixture-not-scheduled")
    saver.accept_turn(session, thread_id=thread, accepted_ingress_id="runtime-new-tail", acceptance_idempotency_key="runtime-tail-accept",
                      job_id="runtime-tail-job", admission_idempotency_key="runtime-tail-admit", payload="最新输入保留在外", payload_kind="text")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    recipe = {"provider_id": "summary", "model_id": "summary", "model": "big-pickle", "endpoint": "https://opencode.ai/zen/v1",
              "api_key": "${COMPACTION_TEST_KEY}", "custom_llm_provider": "openai",
              "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}}}
    preparations = []

    async def prepare_model_recipe(frozen):
        preparations.append(dict(frozen))
        assert frozen == recipe
        return {**frozen, "api_key": "test-key", "proxy_routes": ProxyRoutes.from_configuration(DirectProxy())}

    selected_model = ModelReferenceDTO(provider_id="summary", model_id="summary")

    def resolve_agent_model(agent_id, override):
        assert agent_id == "default" and override == selected_model
        return selected_model

    def freeze_model_recipe(model):
        assert model == selected_model
        return recipe

    catalog = get_session_catalog_components(saver._storage.storage.sessions_dir)
    config = SimpleNamespace(
        prepare_model_recipe=prepare_model_recipe,
        get_snapshot=lambda: recipe,
        use_snapshot=lambda snapshot: nullcontext(snapshot),
        resolve_agent_model=resolve_agent_model,
        freeze_model_recipe=freeze_model_recipe,
        get_compaction_policy=CompactionPolicy,
    )
    store = BackgroundWorkStore(sessions_dir=saver._storage.storage.sessions_dir)
    trace_sink = TraceEventStore(sessions_dir=saver._storage.storage.sessions_dir)
    handler = CompactionTaskHandler(saver=saver, config=config, activation=activation, registry=lambda: registry,
        workspace_id=catalog.resolver.workspace_id, workspace_root=context.workspace_root, session_catalog=catalog, trace_sink=trace_sink)
    registry = BackgroundTaskRegistry(store=store, residency_tracker=ThreadResidencyTracker(), handlers=(handler,))
    runtime = AgentSummarizationCompactor(config_service=config, saver=saver, registry=registry,
                                         sessions=SimpleNamespace(session_read_admission=store.admission))
    try:
        yield runtime, registry, handler, recipe, preparations
    finally:
        await registry.stop()
        await trace_sink.close()


@pytest.mark.asyncio
async def test_runtime_registers_one_real_summary_task_without_holding_session_gate_and_reconciles_cold(
    generation_case, compaction_runtime_case,
):
    saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, handler, recipe, preparations = compaction_runtime_case
    waiting = []
    handler._provider_wait = lambda handle, active: waiting.append((handle.task_id, active))
    owner = MutationIntentOwner(session, thread)
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    async with asyncio.timeout(20):
        handle = await runtime.request(owner, agent_id="default", model_recipe=recipe, deadline=deadline)
        assert await runtime.request(owner, agent_id="another-agent", model_recipe=recipe, foreground=True) == registry.get_handle(session, handle.task_id)
        assert handle.task_id == registry.list_handles(session)[0].task_id
        frozen, result = await runtime.wait(handle)
        while registry.get_handle(session, handle.task_id).status in {"pending", "running"}:
            await asyncio.sleep(0.01)
    assert controller.transport.hit_counts == {0: 1}
    assert preparations == [recipe]
    assert waiting == [(handle.task_id, True), (handle.task_id, False)]
    assert "COMPACTION_TEST_KEY" not in str(handle.descriptor.payload)
    assert "最新输入保留在外" not in str(saver.read_context_for_dispatch(result.dispatch_ref)["snapshot"].request_hash_preimage)
    assert runtime.ready(owner) == ReadyCompactionCandidate(frozen, result, False)
    assert await runtime.request(owner, agent_id="default", model_recipe=recipe) == registry.get_handle(session, handle.task_id)
    recovered = await handler.reconcile(registry.get_handle(session, handle.task_id))
    assert recovered.completed and recovered.result["candidate_id"] == frozen.candidate_id
    assert controller.transport.hit_counts == {0: 1}
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE discarded_reason IS NULL").fetchone() == (1,)


@pytest.mark.asyncio
async def test_runtime_freezes_real_summary_target_and_reuses_same_range_after_retained_input_grows(
    generation_case, compaction_runtime_case, monkeypatch,
):
    import json

    from app.domain.itemized.serialization import normalize_wire_request

    saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, handler, recipe, _preparations = compaction_runtime_case
    owner = MutationIntentOwner(session, thread)
    item_ids = runtime.source_item_ids(owner)
    target = {
        "capacity": {"max_input_tokens": 10000},
        "policy": CompactionPolicy().model_dump(mode="json", exclude={"prepare_fraction"}),
        "retained_input_tokens": 500,
    }
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    requests = []

    async def capture(request, **kwargs):
        requests.append(json.loads(request.content))
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", capture)
    async with asyncio.timeout(30):
        handle = await runtime.request(owner, agent_id="default", item_ids=item_ids, model_recipe=recipe, target_constraint=target)
        frozen, result = await runtime.wait(handle)
        while registry.get_handle(session, handle.task_id).status in {"pending", "running"}:
            await asyncio.sleep(0.01)
    ref, namespace, _deadline = handler.descriptor_input(handle.descriptor)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        persisted = cold._storage.compactions.read_task_input(session, thread, ref, checkpoint_ns=namespace)
        wire = cold.read_context_for_dispatch(result.dispatch_ref)["snapshot"].request_hash_preimage
    assert persisted.target_constraint == target
    assert frozen.target_constraint_hash == sha256_jcs({key: target[key] for key in ("capacity", "policy")})
    assert len(requests) == 1 and normalize_wire_request(requests[0]) == wire
    instruction = str(wire["messages"][-1]["content"])
    assert "55%" in instruction and "soft allowance of 5000 input tokens" in instruction
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0])
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="target-append", acceptance_idempotency_key="target-append-accept",
        job_id="target-append-job", admission_idempotency_key="target-append-admit",
        payload="追加输入改变保留预算，但不能重新生成相同范围。", payload_kind="text",
    )
    reused = await runtime.request(
        owner, agent_id="default", item_ids=item_ids, model_recipe=recipe,
        target_constraint={**target, "retained_input_tokens": 1000},
    )
    assert reused.task_id == handle.task_id
    assert len(registry.list_summaries(session, include_history=True)) == 1 and controller.transport.hit_counts == {0: 1}
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE discarded_reason IS NULL").fetchone() == (1,)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold._storage.compactions.read_task_input(session, thread, ref, checkpoint_ns=namespace).target_constraint == target


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_succeeds", [True, False])
@pytest.mark.parametrize("original_expired", [True, False])
async def test_foreground_retries_original_failed_task_once_with_new_deadline_and_original_budget(
    generation_case, compaction_runtime_case, monkeypatch, retry_succeeds, original_expired,
):
    saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, handler, recipe, _preparations = compaction_runtime_case
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    requests = []

    async def transient_failure(request, **kwargs):
        requests.append(request)
        if len(requests) == 1 or not retry_succeeds:
            return httpx.Response(503, json={"error": {"message": "临时上游错误", "type": "server_error"}}, request=request)
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", transient_failure)
    owner = MutationIntentOwner(session, thread)
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    handle = await runtime.request(owner, agent_id="default", model_recipe=recipe, deadline=deadline)
    await asyncio.gather(registry.get_task(session, handle.task_id), return_exceptions=True)
    failed = registry.get_handle(session, handle.task_id)
    assert failed.status == "failed" and len(requests) == 1
    foreground_deadline = deadline + timedelta(seconds=60)
    if original_expired:
        class ForegroundClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return deadline + timedelta(seconds=1)

        monkeypatch.setattr("app.runtime.context_compaction.datetime", ForegroundClock)
        monkeypatch.setattr("app.services.orchestration.background_work.registry.datetime", ForegroundClock)
        monkeypatch.setattr("app.services.orchestration.background_work.compaction.datetime", ForegroundClock)
    assert connection.execute("SELECT status FROM retention_refs WHERE retention_id=?",
                              (f"compaction:{handle.task_id}",)).fetchone() == ("active",)
    assert await runtime.request(owner, agent_id="default", model_recipe=recipe, foreground=True) == failed
    if retry_succeeds:
        frozen, result = await runtime.wait(failed, deadline=foreground_deadline)
        assert result.coverage == frozen.refs
    else:
        with pytest.raises(RuntimeError, match="compaction-retry-unavailable"):
            await runtime.wait(failed, deadline=foreground_deadline)
        with pytest.raises(RuntimeError, match="compaction-retry-unavailable"):
            await runtime.wait(registry.get_handle(session, handle.task_id), deadline=foreground_deadline + timedelta(seconds=60))
    assert len(requests) == 2
    assert connection.execute("SELECT status FROM retention_refs WHERE retention_id=?",
                              (f"compaction:{handle.task_id}",)).fetchone() == ("active" if retry_succeeds else "released",)
    current = registry.get_handle(session, handle.task_id)
    assert current.task_id == handle.task_id and current.descriptor == handle.descriptor and current.created_at == handle.created_at
    if not retry_succeeds:
        candidate_ids = connection.execute("SELECT candidate_id FROM context_compaction_candidates ORDER BY candidate_id").fetchall()
        for foreground in (False, True):
            assert await runtime.request(owner, agent_id="default", model_recipe=recipe, foreground=foreground) == current
        assert len(requests) == 2
        assert runtime.ready(owner) is None
        assert connection.execute("SELECT candidate_id FROM context_compaction_candidates ORDER BY candidate_id").fetchall() == candidate_ids
    assert current.descriptor.payload["deadline"] == deadline.isoformat()
    assert current.metadata["deadline"] == foreground_deadline.isoformat()
    assert handler.deadline(current) == foreground_deadline
    ref, namespace, first_deadline = handler.descriptor_input(current.descriptor)
    assert first_deadline == deadline
    assert saver._storage.compactions.read_task_input(session, thread, ref, checkpoint_ns=namespace).deadline == deadline
    assert current.lease_id != failed.lease_id and current.fencing_token > failed.fencing_token
    with registry._store.control(session, read_only=True) as control:
        assert control.get_lease(failed.lease_id).state == "failed"
        assert control.get_lease(current.lease_id).operation_identity == f"{handle.task_id}:foreground-retry:1"
    assert connection.execute(
        "SELECT COUNT(*) FROM model_calls call JOIN storage_commits admission ON admission.subject_id=call.execution_id "
        "WHERE admission.commit_kind='execution_admission' AND json_extract(admission.metadata_json,'$.request_input.reference.task_id')=?",
        (handle.task_id,),
    ).fetchone() == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["http", "tool"])
@pytest.mark.parametrize("retry_remaining", [True, False])
async def test_explicit_manual_retry_schedules_real_work_without_resetting_ordinary_preparation(
    generation_case, compaction_runtime_case, monkeypatch, entry, retry_remaining,
):
    from langchain.tools import ToolRuntime

    from app.agents.manual_compaction_checkpoint import (
        ManualCompactionCheckpointAdapter,
    )
    from app.agents.summarization.tool_middleware import CompactionToolsMiddleware
    from app.services.business.context_compaction_service import (
        ContextCompactionService,
    )

    saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, _handler, recipe, _preparations = compaction_runtime_case
    owner = MutationIntentOwner(session, thread)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages_from_dict([
        saver._storage.storage._codec().project_message((item,)) for item in saver.read_canonical_items(session, thread)
    ])}
    checkpoint["channel_versions"] = {"messages": "1"}
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='registry-job'",
    ).fetchone()[0])
    with (session_thread_owner_scope(owner, langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    requests = []
    failures = 1 if retry_remaining else 2

    async def fail_then_succeed(request, **kwargs):
        requests.append(request)
        if len(requests) <= failures:
            return httpx.Response(503, json={"error": {"message": "摘要上游失败", "type": "server_error"}}, request=request)
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", fail_then_succeed)
    original = await runtime.request(owner, agent_id="default", model_recipe=recipe)
    await asyncio.gather(registry.get_task(session, original.task_id), return_exceptions=True)
    if not retry_remaining:
        with pytest.raises(RuntimeError, match="compaction-retry-unavailable"):
            await runtime.wait(original)
    failed = registry.get_handle(session, original.task_id)
    assert failed.status == "failed" and len(requests) == failures
    assert await runtime.request(owner, agent_id="default", model_recipe=recipe) == failed

    if entry == "http":
        async def get_session(_session_id):
            return SimpleNamespace(current_agent_id="default", current_model=ModelReferenceDTO(provider_id="summary", model_id="summary"))

        async def resolve_thread(_session_id, requested):
            assert requested == thread
            return thread

        async def idle_operation(_session_id, _thread_id, operation):
            return await operation()

        service = ContextCompactionService(
            checkpoint_adapter=ManualCompactionCheckpointAdapter(checkpointer=saver),
            job_service=SimpleNamespace(run_thread_idle_operation=idle_operation),
            session_service=SimpleNamespace(session_write_admission=registry._store.admission,
                                            get=get_session, resolve_thread=resolve_thread),
            summarization_compactor=runtime,
        )

        async def invoke_manual():
            scheduled = await service.request_compaction(session, thread_id=thread)
            assert scheduled.status == "scheduled"
            return scheduled.task_id
    else:
        middleware = CompactionToolsMiddleware(compactor=runtime, owner=owner, agent_id="default",
            preferred_model=ModelReferenceDTO(provider_id="summary", model_id="summary"))
        tool_runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None,
                                   tool_call_id="manual-retry", store=None, tools=middleware.tools)

        async def invoke_manual():
            command = await middleware.tools[0].coroutine(tool_runtime)
            content = command.update["messages"][0].content
            return content.split("task_id=", 1)[1].split("。", 1)[0]

    task_id, concurrent_id = await asyncio.gather(invoke_manual(), invoke_manual())
    assert concurrent_id == task_id
    assert (task_id == original.task_id) is retry_remaining
    accepted = registry.get_handle(session, task_id)
    frozen, result = await runtime.wait(accepted)
    assert result.coverage == frozen.refs
    assert len(requests) == failures + 1
    assert runtime.ready(owner) == ReadyCompactionCandidate(frozen, result, True)
    assert connection.execute("SELECT apply_on_next_request FROM context_compaction_candidates WHERE task_id=?",
                              (task_id,)).fetchone() == (1,)
    original_frozen = saver._storage.compactions.read(session, thread, original.task_id)
    assert saver._storage.compactions.call_usage(original_frozen) == (2, 1 if retry_remaining else 2, False)
    if not retry_remaining:
        assert registry.get_handle(session, original.task_id) == failed
        assert saver._storage.compactions.call_usage(frozen) == (1, 0, False)
        assert connection.execute("SELECT discarded_reason FROM context_compaction_candidates WHERE task_id=?",
                                  (original.task_id,)).fetchone() == ("task-failed",)


@pytest.mark.asyncio
async def test_foreground_scope_deadline_cancels_wait_without_extending_inflight_attempt(
    generation_case, compaction_runtime_case, monkeypatch,
):
    from app.core.turn_execution_scope import (
        TurnExecutionScope,
        reset_current_turn_execution_scope,
        set_current_turn_execution_scope,
    )

    saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, handler, recipe, _preparations = compaction_runtime_case
    entered, stopped, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    controller.ensure_installed()

    async def wait_for_upstream(_request, **_kwargs):
        entered.set()
        try:
            await never.wait()
        finally:
            stopped.set()

    monkeypatch.setattr(controller.transport, "handle_async_request", wait_for_upstream)
    original_deadline = datetime.now(UTC) + timedelta(seconds=60)
    handle = await runtime.request(MutationIntentOwner(session, thread), agent_id="default", model_recipe=recipe, deadline=original_deadline)
    async with asyncio.timeout(10):
        await entered.wait()
    scope = TurnExecutionScope("foreground-operation", deadline=time.monotonic() + 0.2)
    token = set_current_turn_execution_scope(scope.child("model-preparation", timeout_seconds=60))
    try:
        with pytest.raises(TimeoutError, match="本次前台操作期限"):
            await runtime.wait(handle, deadline=original_deadline + timedelta(seconds=60))
    finally:
        reset_current_turn_execution_scope(token)
        await scope.close()
    current = registry.get_handle(session, handle.task_id)
    assert stopped.is_set() and current.status == "cancelled"
    assert handler.deadline(current) == original_deadline
    assert saver._storage.compactions.read_result(session, thread, handle.task_id) is None
    assert connection.execute("SELECT status FROM retention_refs WHERE retention_id=?",
                              (f"compaction:{handle.task_id}",)).fetchone() == ("released",)


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True], ids=["cancel", "deadline"])
async def test_abandoned_summary_releases_source_and_rejects_late_sdk_output(
    generation_case, compaction_runtime_case, monkeypatch, expire,
):
    _saver, session, thread, connection, _model, controller, _activation, _freeze = generation_case
    runtime, registry, _handler, recipe, _preparations = compaction_runtime_case
    controller.ensure_installed()
    transport = controller.transport
    dispatch = transport.handle_async_request
    entered, release, late = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def late_response(request):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # 上游已经完成时，取消仍可能收到真实结果；发布必须由原 gate 拒绝。
            late.set()
        return await dispatch(request)

    monkeypatch.setattr(transport, "handle_async_request", late_response)
    owner = MutationIntentOwner(session, thread)
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    handle = await runtime.request(owner, agent_id="default", model_recipe=recipe, deadline=deadline)
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        if expire:
            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return deadline + timedelta(seconds=1)

            with monkeypatch.context() as clock:
                clock.setattr("app.services.orchestration.background_work.registry.datetime", ExpiredClock)
                clock.setattr("app.services.orchestration.background_work.compaction.datetime", ExpiredClock)
                await registry.recover_derived_thread(session, thread)
        else:
            await registry.cancel(session, handle.task_id)
        assert late.is_set()
        assert registry.get_handle(session, handle.task_id).status == ("failed" if expire else "cancelled")
        assert connection.execute("SELECT status FROM retention_refs WHERE retention_id=?",
                                  (f"compaction:{handle.task_id}",)).fetchone() == ("released",)
        assert connection.execute("SELECT discarded_reason,result_detail_ref FROM context_compaction_candidates WHERE task_id=?",
                                  (handle.task_id,)).fetchone() == ("task-failed" if expire else "task-cancelled", None)
        assert runtime.ready(owner) is None
        release.set()
        replacement = await runtime.request(owner, agent_id="default", model_recipe=recipe)
        if expire:
            assert replacement == registry.get_handle(session, handle.task_id)
            assert replacement.status == "failed" and controller.transport.hit_counts == {0: 1}
        else:
            assert replacement.task_id != handle.task_id
            _frozen, result = await runtime.wait(replacement)
            assert result.text
    finally:
        release.set()


@pytest.mark.asyncio
async def test_retry_reuses_completed_chunks_and_counts_the_original_provider_failure(generation_case, monkeypatch):
    saver, _session, _thread, _connection, model, controller, activation, freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 1050}
    candidate = freeze()
    controller.ensure_installed()
    dispatch = controller.transport.handle_async_request
    requests = []

    async def fail_second_chunk(request, **kwargs):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(503, json={"error": {"message": "第二块暂时失败", "type": "server_error"}}, request=request)
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(controller.transport, "handle_async_request", fail_second_chunk)
    generator = CompactionGenerator(saver=saver, model=model, activation=activation)
    deadline = time.time() + 60
    with pytest.raises(Exception) as failure:
        await generator.generate(candidate, budget=SummaryCallBudget(deadline))
    assert getattr(failure.value, "status_code", None) == 503
    calls, failures, retryable = saver._storage.compactions.call_usage(candidate)
    assert (calls, failures, retryable) == (2, 1, True)
    budget = SummaryCallBudget(deadline, calls_used=calls)
    result = await generator.generate(candidate, budget=budget, allow_external_retry=True)
    assert result.coverage == candidate.refs
    assert len(requests) == budget.calls_used == 5 and budget.failure_retries_used == 1
    assert saver._storage.compactions.call_usage(candidate) == (5, 1, False)


@pytest.mark.asyncio
async def test_real_three_chunk_generation_merges_all_sources_and_cold_ready_without_changing_parent(generation_case):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 1050}
    candidate = freeze()
    before = connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall()
    budget = SummaryCallBudget(time.time() + 60)
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=budget)
    assert result.coverage == candidate.refs
    assert budget.calls_used == 4
    assert controller.transport.hit_counts == {0: 4}
    calls = connection.execute(
        "SELECT call.assembly_id FROM model_calls call JOIN executions execution USING(execution_id) "
        "WHERE execution.execution_id IN (SELECT subject_id FROM storage_commits WHERE commit_kind='execution_admission' "
        "AND json_extract(metadata_json,'$.request_input.reference.task_id')=?) ORDER BY call.created_at,call.rowid",
        (candidate.task_id,),
    ).fetchall()
    assert len(calls) == 4
    actual_coverages = []
    for (assembly_id,) in calls:
        snapshot = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
        coverage, text_ranges = saver._storage.compactions._request_coverage(candidate, snapshot)
        assert text_ranges == ()
        actual_coverages.append(coverage)
    assert tuple(ref for coverage in actual_coverages[:3] for ref in coverage) == candidate.refs
    assert actual_coverages[3] == candidate.refs
    assert connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall() == before
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        ready = cold._storage.compactions.read_result(session, thread, candidate.candidate_id)
        assert ready == result
        payload = cold.read_context_for_dispatch(ready.dispatch_ref)
        assert payload["snapshot"].selection_policy == "derived_request"
        selected_summaries = [entry for entry in payload["snapshot"].selection
                              if entry.included and entry.ref.ref_type == "request_only" and entry.ref.semantic_kind == "extension"]
        assert len(selected_summaries) >= 3
    # ready 优先：重复确认任务不产生新模型请求。
    assert await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=budget) == result
    assert budget.calls_used == 4 and controller.transport.hit_counts == {0: 4}


@pytest.mark.asyncio
async def test_ready_uses_actual_sealed_source_dependencies_and_rejects_changed_root(generation_case, production_context):
    saver, session, thread, connection, model, _controller, activation, freeze = generation_case
    manager = production_context[4]
    candidate = freeze(count=1, payload_size=400)
    candidates = saver._storage.compactions
    assert candidates.current_status(session, thread)["status"] == "preparing"
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 60),
    )
    assert result.source_versions == (("instruction:R01", "instructions:1"),)
    assert candidates.current_status(session, thread)["status"] == "ready"
    manager.register(ContextSourceDescriptor(
        source_id="instruction:unread", source_kind="agent_instructions", name="未读取来源",
        description="生成完成后才注册", internal_locator="instruction-producer://unread", root_placement="root_eligible",
    ), tracking_status="tracked")
    manager.observe("instruction:unread", "无关变动", revision="unread:1")
    assert candidates.read_result(session, thread, candidate.candidate_id) == result
    assert candidates.latest_ready(session, thread) == ReadyCompactionCandidate(candidate, result, False)
    manager.observe("instruction:R01", "实际摘要使用的根规则已改变", revision="instructions:2")
    assert candidates.read_result(session, thread, candidate.candidate_id) is None
    assert candidates.latest_ready(session, thread) is None
    assert candidates.reusable_prefix(session, thread, [ref.ref_id for ref in candidate.refs]) is None
    with saver._storage.storage.write_connection(session, thread) as write_connection:
        write_connection.execute("BEGIN IMMEDIATE")
        view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        with pytest.raises(ValueError, match="source-version-changed"):
            candidates.prepare_application(write_connection, ApplyCompactionCandidateIntent(candidate, view))


@pytest.mark.asyncio
async def test_candidate_application_atomically_binds_checkpoint_and_does_not_resurrect_old_input(generation_case, monkeypatch):
    saver, session, thread, connection, model, _controller, activation, freeze = generation_case
    candidate = freeze(count=1, payload_size=400)
    budget = SummaryCallBudget(time.time() + 60)
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=budget)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='registry-job'",
    ).fetchone()[0])
    original = tuple(saver.read_canonical_items(session, thread, item_ids=[ref.ref_id for ref in candidate.refs]))
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="appended-tail", acceptance_idempotency_key="accept-tail",
        job_id="job-tail", admission_idempotency_key="admit-tail", payload="追加后的目标已经变化", payload_kind="text",
    )
    tail = saver.read_canonical_items(session, thread)[-1]
    messages = messages_from_dict([saver._storage.storage._codec().project_message((item,)) for item in original])
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "candidate-parent-checkpoint"
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": "1"}
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
        view_id = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        intent = ApplyCompactionCandidateIntent(candidate, view_id)
        with saver._storage.storage.read_connection(session, thread) as read_connection:
            tail_ref = ContextRef.canonical_item(tail, session_id=session, thread_id=thread)
            assert saver._storage.compactions.select_ready_for_refs(
                read_connection, session, thread, (*candidate.refs, tail_ref),
            ) == result
            assert saver._storage.compactions.select_ready_for_refs(
                read_connection, session, thread, (candidate.refs[0], tail_ref, *candidate.refs[1:]),
            ) is None
            assert saver._storage.compactions.select_ready_for_refs(read_connection, session, thread, ()) is None
        before = connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchone()
        before_size = saver._storage.storage.jsonl_path(session, thread_id=thread).stat().st_size
        original_consume = saver.consume_mutation_intent

        def reject_epoch(*_args):
            raise RuntimeError("injected epoch failure")

        with monkeypatch.context() as patch:
            patch.setattr(saver, "consume_mutation_intent", reject_epoch)
            with pytest.raises(RuntimeError, match="injected epoch failure"):
                saver.adopt_candidate(config, intent)
        assert saver.consume_mutation_intent == original_consume
        assert connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchone() == before
        assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (candidate.candidate_id,)).fetchone() == (None,)
        assert saver._storage.storage.jsonl_path(session, thread_id=thread).stat().st_size == before_size
        applied_tuple = await saver.aadopt_candidate(config, intent)
        assert saver._storage.compactions.current_status(session, thread)["status"] == "applied"
        applied = applied_tuple.config
        assert saver.adopt_candidate(config, intent).config == applied
        assert [message.id for message in applied_tuple.checkpoint["channel_values"]["messages"]] == [f"summary-{candidate.candidate_id}", tail.projection_identity.projection_message_id]
        with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
            restored = cold.get_tuple(applied)
            restored_messages = checkpoint_messages(cold, restored)
            assert len(restored_messages) == 2
            summary = restored_messages[0]
            assert result.text in summary.content and summary.response_metadata["compaction_summary"] is True
        # 模型在采用后失败／取消时，Graph 仍可能提交采用前的内存；这个写入
        # 必须继承已采用基准，不能将被排除的原文选回或丢掉追加尾部。
        stale_checkpoint = {**checkpoint, "id": "stale-graph-after-application", "channel_versions": {"messages": "3"}}
        stale_config = saver.put(config, stale_checkpoint, {"source": "loop", "step": 1}, {"messages": "3"})
        restored = saver.get_tuple(stale_config)
        assert [message.id for message in checkpoint_messages(saver, restored)] == [summary.id, tail.projection_identity.projection_message_id]
    summary_id, applied_view = connection.execute(
        "SELECT applied_summary_item_id,applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (candidate.candidate_id,),
    ).fetchone()
    assert connection.execute(
        "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 ORDER BY context_ordinal", (applied_view,),
    ).fetchall() == [(summary_id,), (tail.item_id,)]
    assert connection.execute("SELECT COUNT(*) FROM context_epoch_transitions WHERE target_view_id=?", (applied_view,)).fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM item_catalog WHERE item_id=?", (summary_id,)).fetchone() == (1,)
    assert tuple(saver.read_canonical_items(session, thread, item_ids=[ref.ref_id for ref in candidate.refs])) == original
    history = saver._storage.compactions.read_history(session, thread)
    assert "原始约束0" in history and "追加后的目标已经变化" not in history
    assert connection.execute("SELECT COUNT(*) FROM compaction_history_ranges").fetchone() == (len(candidate.refs),)
    context_state = await MessageService(main_thread_resolver=lambda _session: thread, checkpointer=saver).get_agent_context_state(session, thread)
    assert context_state["raw_message_count"] == len(original) + 1
    assert context_state["compacted"] is True and len(context_state["records"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor_application", ["never", "before-freeze", "after-ready"])
async def test_successor_uses_frozen_summary_and_atomically_replaces_either_real_selection(generation_case, predecessor_application):
    saver, session, thread, connection, model, controller, activation, freeze = generation_case
    candidates = saver._storage.compactions
    first = freeze(count=1, payload_size=400)
    generator = CompactionGenerator(saver=saver, model=model, activation=activation)
    first_result = await generator.generate(first, budget=SummaryCallBudget(time.time() + 60))
    appended = saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="successor-input", acceptance_idempotency_key="successor-accept",
        job_id="successor-job", admission_idempotency_key="successor-admit", payload="后段明确取消旧计划，改为检查输出", payload_kind="text",
    )
    refs = [*first.refs, ContextRef.canonical_item(saver.read_canonical_items(session, thread, item_ids=(appended["root_input_item_id"],))[0],
                                                 session_id=session, thread_id=thread)]
    original_items = saver.read_canonical_items(session, thread, item_ids=[ref.ref_id for ref in refs])
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "successor-parent-checkpoint"
    checkpoint["channel_values"] = {"messages": messages_from_dict([saver._storage.storage._codec().project_message((item,)) for item in original_items])}
    checkpoint["channel_versions"] = {"messages": "1"}
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='registry-job'",
    ).fetchone()[0])
    with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
          execution_admission_scope(admission, langgraph_thread_id=thread)):
        config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
        if predecessor_application == "before-freeze":
            current_view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
            config = saver.adopt_candidate(config, ApplyCompactionCandidateIntent(first, current_view)).config
        actual_ids = [row[0] for row in connection.execute(
            "SELECT item_id FROM context_view_items WHERE view_id=(SELECT head_view_id FROM branches WHERE status='active') "
            "AND visible=1 AND context_selected=1 ORDER BY context_ordinal",
        )]
        second = candidates.freeze(session, thread, candidate_id="successor", task_id="successor-task", item_ids=actual_ids,
                                   model_config_hash=first.model_config_hash, predecessor_candidate_id=first.candidate_id)
        assert second.refs == tuple(refs)
        assert second.predecessor.result_ref == first_result.result_ref
        assert compaction_selection_ids(second, actual_ids) == tuple(actual_ids)
        assert candidates.predecessor_result(second) == first_result
        second_result = await generator.generate(second, budget=SummaryCallBudget(time.time() + 60), derived_summaries=(first_result,))
        assert second_result.coverage == tuple(refs)
        assert controller.transport.hit_counts == {0: 2}
        wire = saver.read_context_for_dispatch(second_result.dispatch_ref)["snapshot"].request_hash_preimage
        assert first_result.text in str(wire) and "后段明确取消旧计划" in str(wire)
        assert "原始约束0" not in str(wire)
        if predecessor_application == "after-ready":
            current_view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
            config = saver.adopt_candidate(config, ApplyCompactionCandidateIntent(first, current_view)).config
            assert candidates.applicability(session, thread, second.candidate_id) is None
        current_view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        applied = saver.adopt_candidate(config, ApplyCompactionCandidateIntent(second, current_view))
        assert [message.id for message in checkpoint_messages(saver, applied)] == ["summary-successor"]
        context_state = await MessageService(main_thread_resolver=lambda _session: thread, checkpointer=saver).get_agent_context_state(session, thread)
        assert context_state["raw_message_count"] == len(original_items)
        assert len(context_state["records"]) == 1
        assert candidates.predecessor_result(second) == first_result
        history = candidates.read_history(session, thread)
        assert "原始约束0" in history and "后段明确取消旧计划" in history
        assert "[compaction_summary" not in history
        count = connection.execute("SELECT COUNT(*) FROM compaction_history_ranges").fetchone()[0]
        assert count == len(second.refs)
        candidates.read_history(session, thread, line_offset=2, limit=3)
        assert connection.execute("SELECT COUNT(*) FROM compaction_history_ranges").fetchone()[0] == count
        with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
            assert cold._storage.compactions.read_result(session, thread, second.candidate_id) == second_result
            assert cold._storage.compactions.read_history(session, thread) == history


@pytest.mark.asyncio
async def test_oversized_indivisible_source_is_rejected_without_sending_a_suffix(generation_case):
    saver, session, thread, connection, model, controller, activation, _freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 600}
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="indivisible", acceptance_idempotency_key="accept:indivisible",
        job_id="job:indivisible", admission_idempotency_key="admit:indivisible",
        payload=[{"type": "text", "text": "不可剥除媒体后假称全覆盖。" * 1000},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}],
        payload_kind="structured_content",
    )
    candidate = saver._storage.compactions.freeze(
        session, thread, candidate_id="indivisible", task_id="indivisible-task",
        item_ids=[item.item_id for item in saver.read_canonical_items(session, thread)],
        model_config_hash=sha256_jcs({"model": "frozen"}),
    )
    with pytest.raises(ContextCapacityError):
        await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=SummaryCallBudget(time.time() + 60))
    assert not controller.installed
    assert connection.execute("SELECT COUNT(*) FROM model_calls WHERE execution_id LIKE 'summary-execution:%'").fetchone() == (0,)
    assert saver._storage.compactions.read_result(session, thread, candidate.candidate_id) is None


@pytest.mark.asyncio
async def test_insufficient_shared_call_budget_rejects_whole_plan_before_first_request(generation_case):
    saver, session, thread, _connection, model, controller, activation, freeze = generation_case
    model.profile = {**(model.profile or {}), "max_input_tokens": 1050}
    candidate = freeze()
    budget = SummaryCallBudget(time.time() + 60, calls_used=30)
    with pytest.raises(SummaryGenerationError, match="compaction-call-budget-exhausted"):
        await CompactionGenerator(saver=saver, model=model, activation=activation).generate(candidate, budget=budget)
    assert budget.calls_used == 30 and not controller.installed
    assert saver._storage.compactions.read_result(session, thread, candidate.candidate_id) is None
