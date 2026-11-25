"""真实请求 owner、Saver 与 SDK replay 验证独立摘要及冷合并。"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.agents.providers.wire_finalize import chat_wire_profile, chat_wire_params, responses_wire_profile, responses_wire_params
from app.agents.model_call_execution import (
    DerivedModelRequest,
    DerivedModelResult,
    ModelCallExecution,
)
from app.agents.model_call_graph import run_derived_agent
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    InternalExecutionIntent,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from app.testing.model_stream import (
    load_model_stream_config,
    load_scenario,
    responses_frames,
)
from configs.network import DirectProxy
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 原真实来源装具
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
async def summary_model(request, monkeypatch):
    scenario_id, protocol, adapter, suffix, *variants = request.param
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    config = replace(config, transport=replace(config.transport, scenario_id=scenario_id, artifact_root=context.artifacts_dir))
    scenario = load_scenario(config.transport.fixture_root, scenario_id)
    provider_request = scenario.cassette.interactions[0].request
    controller = ModelStreamTransportController.configure(config)
    if variants == ["multi-text"]:
        interaction = scenario.cassette.interactions[0]
        output = [
            {"id": "msg_first", "type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": "前段关键约束。", "annotations": []},
            ]},
            {"id": "rs_between", "type": "reasoning", "summary": [{"type": "summary_text", "text": "核对后续结论。"}]},
            {"id": "msg_last", "type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": "后段保留结论。", "annotations": []},
            ]},
        ]
        frames = responses_frames(response_id="resp_multi", model=provider_request.match["model"], output=output)
        merged_frames = responses_frames(response_id="resp_merged", model=provider_request.match["model"], output=output)
        cassette = replace(scenario.cassette, path=None, interactions=(
            replace(interaction, request=replace(interaction.request, match={**interaction.request.match,
                                                                             "input_types": ["message"] * 3}),
                    response=replace(interaction.response, frames=frames)),
            replace(interaction, index=1,
                    request=replace(interaction.request, match={**interaction.request.match, "input_types": ["message"] * 4}),
                    response=replace(interaction.response, frames=merged_frames)),
        ))
        controller.scenario = replace(scenario, cassette=cassette)

    if variants == ["http-error"]:
        interaction = scenario.cassette.interactions[0]
        response = replace(interaction.response, status=503, headers={"content-type": "application/json"}, frames=())
        controller.scenario = replace(scenario, cassette=replace(scenario.cassette, path=None, interactions=(
            replace(interaction, response=response),
        )))

    if variants in (["empty-output"], ["tool-output"]):
        interaction = scenario.cassette.interactions[0]
        output = [] if variants == ["empty-output"] else [{
            "id": "fc_forbidden", "type": "function_call", "call_id": "forbidden-call", "name": "read_file", "arguments": "{}",
        }]
        frames = responses_frames(response_id="resp_invalid", model=provider_request.match["model"], output=output)
        controller.scenario = replace(scenario, cassette=replace(scenario.cassette, path=None, interactions=(
            replace(interaction, response=replace(interaction.response, frames=frames)),
        )))

    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("派生回放不得创建真实 HTTP transport")

    model = build_model_from_provider(provider={
        "provider_id": "summary", "model_id": "summary", "model": provider_request.match["model"],
        "endpoint": provider_request.url.removesuffix(suffix), "api_key": "test-key", "custom_llm_provider": adapter,
        "api_mode": {"protocol": protocol, "model_info": {}, "supports_reasoning": {}},
        "proxy_routes": ProxyRoutes.from_configuration(DirectProxy()),
    }, runtime_config={}, invocation_purpose="compaction")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden_transport)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden_transport)
    try:
        yield model, controller, protocol
    finally:
        await model.aclose()
        await controller.aclose()


@pytest.fixture
def summary_runner(production_context, summary_model, frozen_provider_activation):
    saver, session, thread, _connection, _manager, _prepare = production_context
    model, _controller, _protocol = summary_model
    owner = ModelCallExecution(checkpointer=saver, session_id=session, thread_id=thread)

    async def run(
        *, candidate, items, derived_summaries=(), stage="chunk", prepare_only=False, source_refs=None,
        suffix="", tools=(), allowed_tool_ids=(), max_model_calls=1, completion_deadline=None,
    ):
        request_id = candidate.candidate_id + suffix
        reference = DerivedExecutionReference(candidate.task_id, f"summary-plan:{request_id}")
        admission = saver.admit_internal_execution(
            session, thread_id=thread, job_id=f"summary-job:{request_id}",
            intent=InternalExecutionIntent(
                message_id=f"summary-message:{request_id}", execution_id=f"summary-execution:{request_id}",
                admission_idempotency_key=f"summary-admission:{request_id}", derived_reference=reference,
            ), message=None,
        )
        plan, preparation, bodies = owner.compile_summary_request(
            reference=reference, checkpoint_ns="", model=model,
            instruction="Summarize the provided conversation. Return only a factual plain-text summary. Do not call tools.",
            items=items, source_refs=(candidate.refs if items else ()) if source_refs is None else source_refs, derived_summaries=derived_summaries,
            source_assembly_id=candidate.source_assembly_id,
        )
        before = _connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()
        preview = owner.preview_summary_request(
            plan=plan, preparation=preparation, bodies=bodies, items=items,
            source_refs=(candidate.refs if items else ()) if source_refs is None else source_refs,
            derived_summaries=derived_summaries, model=model,
        )
        assert _connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == before
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            activation = frozen_provider_activation(saver, plan_creation_idempotency_key=reference.plan_id)
            request = DerivedModelRequest(reference, admission, "", model, plan, preparation, bodies,
                                          activation["activation_snapshot"], activation["model_call_id"])
            if prepare_only:
                return await owner._aprepare_derived(request)
            result = await run_derived_agent(
                request, execution=owner, max_model_calls=max_model_calls, tools=tools,
                allowed_tool_ids=allowed_tool_ids, completion_deadline=completion_deadline,
            )
            assert isinstance(result, DerivedModelResult)
            snapshot = saver.get_context_assembly(session, thread, assembly_id=result.dispatch_ref.assembly_id)
            assert snapshot.request_hash_preimage == preview.wire_request
            return result

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [
    ("basic-text", "chat_completions", "openai", "/chat/completions"),
    ("responses-basic-text", "responses", "openai", "/responses"),
    ("anthropic-reasoning-stream", "anthropic_messages", "anthropic", "/v1/messages"),
], indirect=True)
async def test_chunk_has_its_own_execution_and_keeps_parent_sources_and_open_tool_tail(
    production_context, summary_runner, summary_model,
):
    saver, session, thread, connection, manager, prepare = production_context
    parent = prepare("parent")
    items = tuple(saver.read_canonical_items(session, thread))
    manager.register(ContextSourceDescriptor(
        source_id="instruction:pending", source_kind="agent_instructions", name="尚未消费的父指令",
        description="独立摘要准备期间到达的新来源", internal_locator="instruction-producer://pending",
        root_placement="root_eligible",
    ), tracking_status="tracked")
    manager.observe("instruction:pending", "尚未消费的父指令", revision="instructions:2")
    candidate = saver._storage.compactions.freeze(session, thread, candidate_id="chunk", task_id="task-chunk",
                                                  item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    parent_admission = saver.execution_admission(session, thread_id=thread, execution_id=parent["execution_id"])
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(parent_admission, langgraph_thread_id=thread),
    ):
        saver.ensure_request_items(session, thread, turn_id=parent_admission.turn_id, execution_id=parent_admission.execution_id,
                                   messages=(AIMessage(content="", id="parent-open-tool", response_metadata={"model_call_id": "parent-tool-model"},
                                                       tool_calls=[{"id": "unfinished", "name": "read_file", "args": {}}]),))
    pending = manager.prepare_pending()
    parent_state = connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall()
    parent_execution = connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone()
    result = await summary_runner(candidate=candidate, items=items)
    snapshot = saver.get_context_assembly(session, thread, assembly_id=result.dispatch_ref.assembly_id)
    assert snapshot.execution_id != parent["execution_id"]
    assert snapshot.turn_id is None and snapshot.selection_policy == "derived_request"
    assert snapshot.seal_binding.previous_assembly_id is None and snapshot.seal_binding.source_revisions == ()
    assert all(source.source_binding is None or source.source_binding.source_ref != "instruction:pending"
               for source in snapshot.contributions)
    activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=snapshot.assembly_id,
        plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
    )
    assert snapshot.tool_snapshot == () and activation.bindings == ()
    assert activation.execution_id == snapshot.execution_id
    assert snapshot.request_hash_preimage.get("tools") in (None, [])
    assert not any(key in snapshot.request_hash_preimage for key in ("tool_choice", "parallel_tool_calls", "response_format"))
    assert connection.execute("SELECT outcome FROM executions WHERE execution_id=?", (snapshot.execution_id,)).fetchone() == ("completed",)
    assert connection.execute("SELECT outcome,dispatch_state FROM model_calls WHERE execution_id=?", (snapshot.execution_id,)).fetchone() == ("completed", "completed")
    assert result.output_refs
    output, = saver.read_canonical_items(session, thread, item_ids=[result.output_refs[0].ref_id])
    assert output.turn_id is None
    assert connection.execute("SELECT execution_id FROM item_catalog WHERE item_id=?", (output.item_id,)).fetchone() == (snapshot.execution_id,)
    assert output.projection_identity.model_call_id == result.dispatch_ref.callback_model_call_id
    assert manager.prepare_pending() == pending
    assert connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone() == parent_execution
    assert connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall() == parent_state
    assert connection.execute("SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='tool_result'").fetchone() == (0,)
    with saver._context_reader.open_snapshot(session, thread, "") as read_snapshot:
        assert saver._storage.assemblies.latest_context_assembly(read_snapshot).assembly_id == parent["assembly_id"]
    assert summary_model[1].transport.hit_counts == {0: 1}
    log_paths = tuple((get_session_path_resolver(saver._storage.storage.sessions_dir).resolve_session_node(session) / "logs/llm_requests").glob("*.json"))
    log, = (json.loads(path.read_text()) for path in log_paths)
    assert log["request"]["purpose"] == "compaction"
    assert log["request"]["execution_id"] == snapshot.execution_id
    assert log["request"]["turn_id"] is None
    assert log["request"]["derived_reference"]["task_id"] == candidate.task_id
    assert len(log["upstream"]["attempts"]) == 1
    detail_ref = saver._storage.compactions.publish_result(candidate, output_refs=result.output_refs, dispatch_ref=result.dispatch_ref)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        ready = cold._storage.compactions.read_result(session, thread, candidate.candidate_id)
        assert ready.result_ref == detail_ref
        assert ready.text == result.response.result[0].text
        assert cold.read_context_for_dispatch(
            result.dispatch_ref,
            wire_profile=responses_wire_profile(summary_model[0]) if result.dispatch_ref.target_format == "responses" else chat_wire_profile(summary_model[0]),
            wire_params=responses_wire_params(summary_model[0], kwargs={}) if result.dispatch_ref.target_format == "responses" else chat_wire_params(summary_model[0], stop=None, kwargs={}, stream=True),
        )["snapshot"] == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [("responses-basic-text", "responses", "openai", "/responses", "multi-text")], indirect=True)
async def test_merge_uses_real_result_and_rejects_forged_text_after_cold_read(production_context, summary_runner, summary_model):
    saver, session, thread, _connection, _manager, prepare = production_context
    prepare("parent")
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="merge-source", acceptance_idempotency_key="merge-accept",
        job_id="merge-source-job", admission_idempotency_key="merge-source-admit", payload="后续历史约束", payload_kind="text",
    )
    items = tuple(saver.read_canonical_items(session, thread))
    owner = saver._storage.compactions
    candidate = owner.freeze(session, thread, candidate_id="first", task_id="task-first",
                             item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    first = await summary_runner(candidate=candidate, items=items)
    assert len(first.output_refs) == 2
    with pytest.raises(ValueError, match="完整有序输出"):
        owner.publish_result(candidate, output_refs=first.output_refs[:1], dispatch_ref=first.dispatch_ref)
    owner.publish_result(candidate, output_refs=first.output_refs, dispatch_ref=first.dispatch_ref)
    ready = owner.read_result(session, thread, candidate.candidate_id)
    assert ready.text == "前段关键约束。后段保留结论。"
    merge_candidate = owner.freeze(session, thread, candidate_id="merge", task_id="task-merge",
                                   item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    merged = await summary_runner(candidate=merge_candidate, items=(), derived_summaries=(ready,), stage="merge")
    owner.publish_result(merge_candidate, output_refs=merged.output_refs, dispatch_ref=merged.dispatch_ref)
    owner.discard(session, thread, candidate.candidate_id, reason="merged")
    saver.gc_context_plan_details(session, thread, expired_before=datetime.max.replace(tzinfo=UTC))
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        payload = cold.read_context_for_dispatch(
            merged.dispatch_ref,
            wire_profile=responses_wire_profile(summary_model[0]) if merged.dispatch_ref.target_format == "responses" else chat_wire_profile(summary_model[0]),
            wire_params=responses_wire_params(summary_model[0], kwargs={}) if merged.dispatch_ref.target_format == "responses" else chat_wire_params(summary_model[0], stop=None, kwargs={}, stream=True),
        )
        assert cold._storage.compactions.read_result(session, thread, merge_candidate.candidate_id).text == ready.text
        assert any(ready.text in message.content and message.response_metadata.get("compaction_summary")
                   for message in payload["messages"])
        canonical = [entry for entry in payload["snapshot"].selection if entry.ref.ref_type == "canonical_item"]
        assert canonical and all(not entry.included for entry in canonical)
    forged_candidate = owner.freeze(session, thread, candidate_id="forged", task_id="task-forged",
                                    item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    before = summary_model[1].transport.call_count
    with pytest.raises(ValueError, match="immutable identity/hash 冲突"):
        await summary_runner(candidate=forged_candidate, items=(), derived_summaries=(replace(ready, text="伪造摘要"),), stage="merge")
    assert summary_model[1].transport.call_count == before


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
async def test_partial_chunks_cannot_publish_whole_range_before_full_merge(production_context, summary_runner):
    saver, session, thread, connection, _manager, prepare = production_context
    prepare("parent")
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="second-input", acceptance_idempotency_key="second-accept",
        job_id="second-job", admission_idempotency_key="second-admit", payload="后续完整用户约束", payload_kind="text",
    )
    items = tuple(saver.read_canonical_items(session, thread))
    assert len(items) == 2
    owner = saver._storage.compactions
    frozen = owner.freeze(session, thread, candidate_id="chunks", task_id="task-chunks",
                          item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    results = []
    for index in range(2):
        chunk = await summary_runner(candidate=frozen, items=items[index:index + 1],
                                     source_refs=frozen.refs[index:index + 1], suffix=f":{index}")
        with pytest.raises(ValueError, match="完整覆盖冻结范围"):
            owner.publish_result(frozen, output_refs=chunk.output_refs, dispatch_ref=chunk.dispatch_ref)
        saved = owner.save_summary_result(frozen, output_refs=chunk.output_refs, dispatch_ref=chunk.dispatch_ref)
        assert saved.coverage == frozen.refs[index:index + 1]
        assert owner.read_result(session, thread, frozen.candidate_id) is None
        results.append(saved)
    merged = await summary_runner(candidate=frozen, items=(), derived_summaries=tuple(results), stage="merge", suffix=":merge")
    owner.publish_result(frozen, output_refs=merged.output_refs, dispatch_ref=merged.dispatch_ref)
    assert owner.read_result(session, thread, frozen.candidate_id).coverage == frozen.refs
    assert connection.execute("SELECT COUNT(*) FROM context_compaction_candidates WHERE result_detail_ref IS NOT NULL").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [
    ("basic-text", "chat_completions", "openai", "/chat/completions", "http-error"),
    ("responses-basic-text", "responses", "openai", "/responses", "http-error"),
    ("anthropic-reasoning-stream", "anthropic_messages", "anthropic", "/v1/messages", "http-error"),
], indirect=True)
async def test_one_failed_derived_call_has_no_hidden_sdk_retry(production_context, summary_runner, summary_model):
    saver, session, thread, connection, _manager, prepare = production_context
    parent = prepare("parent")
    before = connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone()
    items = tuple(saver.read_canonical_items(session, thread))
    frozen = saver._storage.compactions.freeze(session, thread, candidate_id="failure", task_id="task-failure",
                                               item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    with pytest.raises(Exception) as failure:
        await summary_runner(candidate=frozen, items=items)
    assert getattr(failure.value, "status_code", None) == 503
    assert summary_model[1].transport.call_count == 1
    assert connection.execute("SELECT outcome FROM executions WHERE execution_id='summary-execution:failure'").fetchone() == ("failed",)
    assert connection.execute("SELECT outcome,dispatch_state FROM model_calls WHERE execution_id='summary-execution:failure'").fetchone() == ("failed", "failed")
    assert connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone() == before
    assert saver._storage.compactions.read_result(session, thread, frozen.candidate_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [
    ("responses-basic-text", "responses", "openai", "/responses", "empty-output"),
    ("responses-basic-text", "responses", "openai", "/responses", "tool-output"),
], indirect=True)
async def test_invalid_summary_output_fails_its_own_execution_without_publishing(
    production_context, summary_runner, summary_model, request,
):
    saver, session, thread, connection, _manager, prepare = production_context
    parent = prepare("parent")
    before = connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone()
    items = tuple(saver.read_canonical_items(session, thread))
    frozen = saver._storage.compactions.freeze(session, thread, candidate_id="invalid", task_id="task-invalid",
                                               item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    with pytest.raises(ValueError, match="派生图未生成完整文本答案|derived-call-budget-exhausted"):
        await summary_runner(candidate=frozen, items=items)
    assert summary_model[1].transport.call_count == 1
    assert connection.execute("SELECT outcome FROM executions WHERE execution_id='summary-execution:invalid'").fetchone() == ("failed",)
    assert connection.execute("SELECT outcome,dispatch_state FROM model_calls WHERE execution_id='summary-execution:invalid'").fetchone() == ("completed", "completed")
    items = connection.execute(
        "SELECT semantic_kind,status FROM item_catalog WHERE execution_id='summary-execution:invalid' ORDER BY item_sequence"
    ).fetchall()
    expected_kinds = (("assistant_output",) if request.node.callspec.params["summary_model"][-1] == "empty-output"
                      else ("tool_call", "tool_result"))
    assert tuple(row[0] for row in items) == expected_kinds
    assert all(row[1] == "completed" for row in items)
    assert connection.execute("SELECT * FROM executions WHERE execution_id=?", (parent["execution_id"],)).fetchone() == before
    assert saver._storage.compactions.read_result(session, thread, frozen.candidate_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
async def test_cancelled_prepare_waits_for_worker_before_terminal_convergence(
    production_context, summary_runner, summary_model, monkeypatch,
):
    saver, session, thread, connection, _manager, prepare = production_context
    parent = prepare("parent")
    items = tuple(saver.read_canonical_items(session, thread))
    frozen = saver._storage.compactions.freeze(session, thread, candidate_id="cancel", task_id="task-cancel",
                                               item_ids=[item.item_id for item in items], model_config_hash=sha256_jcs({}))
    entered, release = threading.Event(), threading.Event()
    original = saver.freeze_context_for_provider

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("测试未释放准备 worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(saver, "freeze_context_for_provider", blocked)
    task = asyncio.create_task(summary_runner(candidate=frozen, items=items))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.execute("SELECT outcome FROM executions WHERE execution_id='summary-execution:cancel'").fetchone() == ("cancelled",)
    assert connection.execute("SELECT status,outcome FROM context_assemblies WHERE execution_id='summary-execution:cancel'").fetchone() == ("terminal", "cancelled")
    assert not any(key[3] == "summary-execution:cancel" for key in saver._prepared_dispatches)
    assert not summary_model[1].installed or summary_model[1].transport.call_count == 0
    with saver._context_reader.open_snapshot(session, thread, "") as read_snapshot:
        assert saver._storage.assemblies.latest_context_assembly(read_snapshot).assembly_id == parent["assembly_id"]
