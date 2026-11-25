"""真实冻结预算与稳定 sink/source 提交在没有下一请求时触发后台准备。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope
from langchain_core.messages import AIMessage, AIMessageChunk

from app.agents.providers.request_budget import estimate_wire_input
from app.core.model_delta_context import (
    AgentToolExecutionCallbackHandler,
    model_tool_invocation_scope,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
)
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.message_stream.runtime import MessageStreamRuntime
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    compaction_runtime_case as compaction_runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    generation_case as generation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    model_preparation_case as model_preparation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    production_context as production_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    registry_db as registry_db,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_preparation import (
    summary_model as summary_model,  # noqa: PLC0414
)

pytestmark = [
    pytest.mark.parametrize("registry_db", ["protected"], indirect=True),
    pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True),
]


@pytest.fixture
async def observation_case(model_preparation_case):
    execution, incoming, model, tokens, enabled, compactor, registry, connection, controller, session, thread = model_preparation_case
    owner = MutationIntentOwner(session, thread)
    scope = LifetimeScope("stable-context-observation")
    compactor.bind_observation(owner, scope)
    enabled[0] = True
    model.profile = {**model.profile, "max_input_tokens": int(tokens / 0.70)}
    with execution.graph_request_scope():
        baseline = await execution._aprepare(incoming("observation-baseline"))
    assert registry.list_handles(session) == []
    service = AgentExecutionService.__new__(AgentExecutionService)
    service._dependency_provider = SimpleNamespace(
        get_summarization_compactor=lambda: compactor,
        get_mcp_catalog_owner=lambda: SimpleNamespace(execution_handles=SimpleNamespace(acknowledge_tool_results=Mock())),
    )
    service._background_mailbox = SimpleNamespace(acknowledge_tool_results=Mock())
    sink = CanonicalItemSink(execution._checkpointer, session_id=session, thread_id=thread, checkpoint_ns="",
                             committed_item_observer=service._acknowledge_tool_results)
    try:
        yield execution, incoming, compactor, registry, connection, controller, owner, scope, sink, baseline, enabled
    finally:
        await scope.close()


async def _settle_observation(compactor, owner):
    async with asyncio.timeout(10):
        while True:
            scope = compactor._preparation_scopes[owner]
            if scope.observation_task is None or scope.observation_dirty and scope.observation_task.done():
                await asyncio.sleep(0)
                continue
            await scope.observation_task
            if not scope.observation_dirty:
                return


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks", [1, 230])
async def test_real_stable_reply_sink_starts_one_candidate_without_next_request(observation_case, blocks):
    execution, incoming, compactor, registry, connection, controller, owner, scope, sink, baseline, _enabled = observation_case
    admission = execution._request_preparation(incoming("reply-identity")).admission
    before = connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (admission.execution_id,)).fetchone()
    items = LangChainMessageCodec().items_for_message(
        AIMessage(content=[{"type": "text", "text": "稳定回复正文带来新的上下文开销。" * (1600 // blocks + 1)}
                           for _ in range(blocks)], id="stable-observed-reply"),
        item_sequence=1, message_id="stable-observed-reply", turn_id=admission.turn_id,
        execution_id=admission.execution_id, timestamp=datetime.now(UTC).isoformat(),
    )
    assert len(items) >= blocks
    await sink.append(items)
    for _ in range(100):
        compactor.observe_committed(owner)
    await _settle_observation(compactor, owner)
    handles = registry.list_handles(owner.session_id)
    assert len(handles) == 1
    async with asyncio.timeout(30):
        frozen, _result = await compactor.wait(handles[0])
    assert controller.transport.hit_counts == {0: 1}
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (frozen.candidate_id,)).fetchone() == (None,)
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (admission.execution_id,)).fetchone() == before
    assert compactor._preparation_scopes[owner].observation_budget.estimate.known_tokens > estimate_wire_input(baseline["snapshot"].request_hash_preimage).known_tokens
    await scope.close()
    compactor.observe_committed(owner)
    assert owner not in compactor._preparation_scopes


@pytest.mark.asyncio
async def test_real_tool_result_commit_crosses_threshold_without_next_request(observation_case):
    execution, incoming, compactor, registry, connection, controller, owner, _scope, sink, baseline, _enabled = observation_case
    admission = execution._request_preparation(incoming("tool-observation")).admission
    before = connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (admission.execution_id,)).fetchone()

    async def register(model_call_id, attempt, provider):
        execution._checkpointer.register_model_call(
            owner.session_id, thread_id=owner.thread_id, execution_id=admission.execution_id,
            model_call_id=model_call_id, attempt=attempt, provider=provider, assembly_id=baseline["assembly_id"],
        )
        return admission.execution_id

    runtime = MessageStreamRuntime(
        None, canonical_item_sink=sink.append, tool_result_sink=sink.complete_tool_result,
        canonical_turn_id=admission.turn_id, model_call_registrar=register,
    )
    model_call_id = baseline["snapshot"].model_call_id
    await runtime.start_model(model_call_id, "stable-observation-provider")
    await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[{
        "index": 0, "id": "stable-tool", "name": "read_file", "args": '{"path":"README.md"}',
    }]))
    assert registry.list_handles(owner.session_id) == []
    await runtime.finish_model()
    await _settle_observation(compactor, owner)
    assert registry.list_handles(owner.session_id) == []
    timing = AgentToolExecutionCallbackHandler(runtime)
    with model_tool_invocation_scope(model_call_id, "stable-tool"):
        timing.on_tool_start(run_id="stable-tool-attempt", tool_call_id="stable-tool")
    binding = await runtime.tool_binding_for_callback("stable-tool-attempt")
    await runtime.start_tool(tool_execution_id="stable-tool-attempt", tool_call_id=binding.tool_call_id,
                             tool_name="read_file", started_at=binding.started_at)
    timing.on_tool_end(run_id="stable-tool-attempt")
    await runtime.complete_tool(tool_execution_id="stable-tool-attempt", tool_call_id=binding.tool_call_id,
                                tool_name="read_file", status="succeeded", result="稳定工具正文新增上下文开销。" * 1600)
    await _settle_observation(compactor, owner)
    handles = registry.list_handles(owner.session_id)
    assert len(handles) == 1
    async with asyncio.timeout(30):
        frozen, _result = await compactor.wait(handles[0])
    assert controller.transport.hit_counts == {0: 1}
    assert connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (frozen.candidate_id,)).fetchone() == (None,)
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (admission.execution_id,)).fetchone() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["instruction:R01", "instruction:tail"])
async def test_source_acceptance_reassesses_original_compiler_without_seal_or_consumption(observation_case, production_context, monkeypatch, source_id):
    execution, incoming, compactor, registry, connection, controller, owner, _scope, _sink, baseline, enabled = observation_case
    manager = production_context[4]
    manager._lifecycle_event_sink = lambda event: compactor.invalidate_observation(owner) if event.kind == "reconcile" else None
    if source_id == "instruction:tail":
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name="尾部来源",
            description="尾部来源", internal_locator="instruction-producer://tail", root_placement="tail_only",
        ), tracking_status="tracked")
        manager.observe(source_id, "基础规则第一版", revision="instructions:1")
        await _settle_observation(compactor, owner)
    controller.ensure_installed()
    requests = []
    original_request = controller.transport.handle_async_request

    async def capture_request(request):
        requests.append(json.loads(request.content))
        return await original_request(request)

    monkeypatch.setattr(controller.transport, "handle_async_request", capture_request)
    projections = []
    original_reassess = compactor._preparation_scopes[owner].observation_reassess

    def capture_readonly(items):
        observed = original_reassess(items)
        projections.append(observed[0].wire_request)
        return observed

    compactor._preparation_scopes[owner].observation_reassess = capture_readonly
    original_execution = baseline["snapshot"].execution_id
    before = connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (original_execution,)).fetchone()
    manager.observe(source_id, "来源稳定更新后的完整规则。\n" * 2500)
    pending = manager.read_sources()
    states = execution._checkpointer.load_context_source_control_states(manager._owner)
    await _settle_observation(compactor, owner)
    assert manager.read_sources() == pending
    assert execution._checkpointer.load_context_source_control_states(manager._owner) == states
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (original_execution,)).fetchone() == before
    handles = registry.list_handles(owner.session_id)
    assert len(handles) == 1
    async with asyncio.timeout(30):
        await compactor.wait(handles[0])
    assert controller.transport.hit_counts == {0: 1}
    assert "来源稳定更新后的完整规则。" in json.dumps(requests, ensure_ascii=False)
    # 来源回到已提交基准后必须整体重评，不能继续单向累加旧来源预算。
    large = compactor._preparation_scopes[owner].observation_budget.estimate.known_tokens
    manager.observe(source_id, "基础规则第一版", revision="instructions:1")
    await asyncio.sleep(0)
    await _settle_observation(compactor, owner)
    assert compactor._preparation_scopes[owner].observation_budget.estimate.known_tokens < large
    manager.observe(source_id, "来源稳定更新后的完整规则。\n" * 2500)
    await _settle_observation(compactor, owner)
    actual_request = incoming("actual-after-source-observation")
    actual_request.model.profile = {**actual_request.model.profile, "max_input_tokens": 100000}
    with execution.graph_request_scope():
        actual = await execution._aprepare(actual_request)
    assert actual["snapshot"].request_hash_preimage == projections[-1]
    before_disabled = connection.execute("SELECT candidate_id, task_id FROM context_compaction_candidates ORDER BY candidate_id").fetchall()
    enabled[0] = False
    manager.observe(source_id, "自动压缩关闭后的新来源。" * 2500)
    await asyncio.sleep(0)
    await _settle_observation(compactor, owner)
    assert connection.execute("SELECT candidate_id, task_id FROM context_compaction_candidates ORDER BY candidate_id").fetchall() == before_disabled
    assert controller.transport.hit_counts == {0: 1}
