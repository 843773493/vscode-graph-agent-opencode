from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from app.agents import model_call_execution
from app.agents.model_call_execution import ModelCallExecution
from app.core.identifier import uuid7_datetime_from_hex
from app.core.model_request_context import (
    SummaryCandidateScope,
    summary_candidate_scope,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@pytest.fixture
def bridge(registered_tool_set_producer_factory):
    return ModelCallExecution(
        context_read_admission=nullcontext,
        context_control_admission=lambda _operation, _payload, _ref: nullcontext(),
        checkpointer=object(), session_id="session-unit", thread_id="thread-unit",
        context_source_manager=ContextSourceManager(),
        tool_set_producer=registered_tool_set_producer_factory(),
        resource_activation=None,
    )


@pytest.fixture
def response_dispatch(bridge, monkeypatch):
    model_call_id = "00000000-0000-4000-8000-000000000001"
    prepared = {"dispatch_ref": object(), "snapshot": object(), "resource_refs": (), "callback_model_call_id": model_call_id}
    bridge._prepare_projection = MagicMock(return_value=(prepared, [], []))
    bridge._aprepare = AsyncMock(return_value=prepared)
    bridge._invoke = MagicMock(return_value=ModelResponse(result=[]))
    bridge._ainvoke = AsyncMock(return_value=ModelResponse(result=[]))
    bridge._project_prepared = MagicMock(return_value=(prepared, [], []))
    monkeypatch.setattr(
        model_call_execution, "sealed_assembly_dispatch_scope",
        lambda *_args, **_kwargs: nullcontext(),
    )

    async def dispatch(response, mode):
        request = ModelRequest(model=MagicMock(), messages=[])
        bridge._invoke.return_value = response
        bridge._ainvoke.return_value = response
        if mode == "sync":
            return bridge.execute(request)

        return await bridge.aexecute(request)

    return model_call_id, dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("extended", [False, True])
async def test_vendor_carrier_binds_emitting_call_and_its_structured_tool_result(
    response_dispatch, mode, extended,
):
    model_call_id, dispatch = response_dispatch
    assistant = AIMessage(
        content="工具回答", id="resp-vendor",
        response_metadata={"provider_id": "test-provider"},
        tool_calls=[{"id": "call-vendor", "name": "answer", "args": {"value": "ok"}}],
    )
    result = ToolMessage(
        content="ok", id="result-vendor", tool_call_id="call-vendor", name="answer",
    )
    structured = {"value": "ok"}
    response = ModelResponse(result=[assistant, result], structured_response=structured)
    command = Command(update={"selected": "unchanged"})
    wrapped = ExtendedModelResponse(model_response=response, command=command) if extended else response

    bound = await dispatch(wrapped, mode)
    if extended:
        assert isinstance(bound, ExtendedModelResponse)
        assert bound.command is command
        bound = bound.model_response
    assert bound.structured_response is structured
    bound_assistant, bound_result = bound.result
    assert isinstance(bound_assistant, AIMessage)
    assert isinstance(bound_result, ToolMessage)
    assert bound_assistant.id == assistant.id == "resp-vendor"
    assert bound_assistant.content == assistant.content
    assert bound_assistant.tool_calls == assistant.tool_calls
    assert bound_assistant.response_metadata == {
        "provider_id": "test-provider", "model_call_id": model_call_id,
    }
    assert bound_result.id == result.id == "result-vendor"
    assert bound_result.tool_call_id == result.tool_call_id == "call-vendor"
    assert bound_result.content == "ok"
    assert bound_result.response_metadata == {"model_call_id": model_call_id}
    assert assistant.response_metadata == {"provider_id": "test-provider"}
    assert result.response_metadata == {}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_model_dispatch_reserves_before_sending_and_retains_only_actual_calls(bridge, response_dispatch, mode):
    _model_call_id, dispatch = response_dispatch
    steps = []

    @contextmanager
    def qualification():
        steps.append("reserved")
        try:
            yield lambda calls: steps.append(("retained", calls))
        finally:
            steps.append("exited")

    prepared = bridge._prepare_projection.return_value[0]
    prepared["extension_call_scope"] = qualification()
    call = {"id": "actual-call", "name": "invoke_extension_tool", "args": {"tool_name": "mcp__mini__echo"}}
    response = ModelResponse(result=[AIMessage(content="", tool_calls=[call])])

    def sent(_prepared, _messages, _tools):
        steps.append("sent")
        return response

    async def asent(*args):
        return sent(*args)

    bridge._invoke.side_effect = sent
    bridge._ainvoke.side_effect = asent
    result = await dispatch(response, mode)
    assert steps == ["reserved", "sent", ("retained", result.result[0].tool_calls), "exited"]


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_failed_connection_qualification_prevents_model_sending(bridge, response_dispatch, mode):
    _model_call_id, dispatch = response_dispatch

    @contextmanager
    def rejected_qualification():
        raise OSError("测试资格写盘失败")
        yield  # pragma: no cover - contextmanager 的生成器合同。

    bridge._prepare_projection.return_value[0]["extension_call_scope"] = rejected_qualification()
    with pytest.raises(OSError, match="资格写盘失败"):
        await dispatch(ModelResponse(result=[]), mode)
    bridge._invoke.assert_not_called()
    bridge._ainvoke.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("summary", [False, True])
async def test_request_tool_result_phase_precedes_preparation(
    bridge, response_dispatch, monkeypatch, mode, summary,
):
    model_call_id, _dispatch = response_dispatch
    call = {"id": "call-queued", "name": "read_file", "args": {"path": "README.md"}}
    messages = [
        AIMessage(content="", id=f"lc_run--{model_call_id}", tool_calls=[call]),
        ToolMessage(content="真实 README", tool_call_id=call["id"]),
    ]
    request = ModelRequest(model=MagicMock(), messages=[HumanMessage(content="仅摘要早期历史")] if summary else messages)
    candidate = SummaryCandidateScope(0, "initial", True, False, canonical_messages=tuple(messages))
    observed = []
    phases = []
    sync_projection = bridge._prepare_projection.return_value

    def prepare_sync(_request):
        phases.append("prepare")
        return sync_projection

    async def prepare_async(_request):
        phases.append("prepare")
        return bridge._aprepare.return_value

    bridge._prepare_projection.side_effect = prepare_sync
    bridge._aprepare.side_effect = prepare_async

    def observe(operation_id, phase):
        assert "prepare" not in phases
        phases.append("observe")
        observed.append((operation_id, phase))

    async def observe_async(operation_id, phase):
        observe(operation_id, phase)

    monkeypatch.setattr(model_call_execution, "observe_phase_sync", observe)
    monkeypatch.setattr(model_call_execution, "observe_phase", observe_async)
    async def reconcile_result(_message, *, model_call_id):
        assert "prepare" not in phases
        phases.append("reconcile")

    reconcile = AsyncMock(side_effect=reconcile_result)
    monkeypatch.setattr(
        model_call_execution, "get_current_model_delta_sink",
        lambda: SimpleNamespace(complete_tool_from_message=reconcile),
    )
    with summary_candidate_scope(candidate) if summary else nullcontext():
        if mode == "sync":
            bridge.execute(request)
            assert phases == ["observe", "prepare"]
            reconcile.assert_not_awaited()
        else:
            await bridge.aexecute(request)
            reconcile.assert_awaited_once_with(messages[1], model_call_id=model_call_id)
            assert phases == ["observe", "reconcile", "prepare"]
    assert observed == [(f"{model_call_id}:tool-call:call-queued", "canonical.tool_result.before_commit")]
    assert LangChainMessageCodec().tool_result_call_ids([
        messages[0], messages[1],
        messages[1].model_copy(update={"tool_call_id": f"{model_call_id}:tool-call:call-queued"}),
    ]) == (f"{model_call_id}:tool-call:call-queued",)


@pytest.mark.asyncio
async def test_request_reconciliation_requires_explicit_emitting_model_call(
    bridge, response_dispatch, monkeypatch,
):
    reconcile = AsyncMock()
    monkeypatch.setattr(
        model_call_execution, "get_current_model_delta_sink",
        lambda: SimpleNamespace(complete_tool_from_message=reconcile),
    )
    request = ModelRequest(model=MagicMock(), messages=[ToolMessage(content="结果", tool_call_id="unbound")])
    handler = bridge._ainvoke
    with pytest.raises(RuntimeError, match="emitting model-call identity"):
        await bridge.aexecute(request)
    reconcile.assert_not_awaited()
    bridge._aprepare.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_preparation_failure_preserves_reconciled_tool_completion(
    bridge, response_dispatch, monkeypatch,
):
    model_call_id, _dispatch = response_dispatch
    events = []
    writer = MagicMock()

    async def commit(event_type, payload=None, **kwargs):
        events.append(event_type)
        return {"type": event_type, "payload": payload, "kwargs": kwargs}

    writer.commit = AsyncMock(side_effect=commit)
    tool_result_sink = AsyncMock(
        side_effect=lambda _completion: events.append("tool_result_sink")
    )
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=AsyncMock(),
        tool_result_sink=tool_result_sink,
    )
    await runtime.start_model(model_call_id, "primary")
    await runtime.accept_message_chunk(AIMessageChunk(
        content="",
        tool_call_chunks=[{
            "index": 0,
            "id": "call",
            "name": "read_file",
            "args": '{"path":"README.md"}',
        }],
    ))
    events.clear()

    async def fail_preparation(_request):
        events.append("prepare")
        raise RuntimeError("原 canonical preparation 失败")

    bridge._aprepare.side_effect = fail_preparation
    monkeypatch.setattr(
        model_call_execution,
        "get_current_model_delta_sink",
        lambda: runtime,
    )
    request = ModelRequest(model=MagicMock(), messages=[
        AIMessage(content="", id=f"lc_run--{model_call_id}",
                  tool_calls=[{
                      "id": "call",
                      "name": "read_file",
                      "args": {"path": "README.md"},
                  }]),
        ToolMessage(content="原结果", tool_call_id="call", name="read_file"),
    ])
    handler = bridge._ainvoke
    preparation = asyncio.create_task(bridge.aexecute(request))
    await asyncio.sleep(0)
    assert not preparation.done()
    tool_call_id = runtime.claim_tool_call_id(model_call_id, "call")
    assert tool_call_id == f"{model_call_id}:tool-call:call"
    await runtime.start_tool(
        tool_execution_id="execution_a",
        tool_call_id=tool_call_id,
        tool_name="read_file",
    )
    with pytest.raises(RuntimeError, match="原 canonical preparation 失败"):
        await preparation

    assert events == [
        "tool_call.completed",
        "tool.started",
        "tool_result_sink",
        "tool.completed",
        "prepare",
    ]
    terminal = next(
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool.completed"
    )
    assert terminal["tool_execution_id"] == "execution_a"
    assert terminal["tool_attempt_id"] == "execution_a"
    assert terminal["tool_call_id"] == tool_call_id
    assert terminal["completion_reason"] == "tool_completed"
    tool_result_sink.assert_awaited_once()
    bridge._aprepare.assert_awaited_once_with(request)
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("conflict", ["assistant", "nested", "lc_id", "tool", "unpaired"])
async def test_response_identity_conflicts_fail_without_rebinding(
    response_dispatch, mode, conflict,
):
    model_call_id, dispatch = response_dispatch
    assistant = AIMessage(
        content="", id="resp-vendor",
        tool_calls=[{"id": "call-vendor", "name": "answer", "args": {}}],
    )
    result = ToolMessage(content="ok", tool_call_id="call-vendor", name="answer")
    if conflict == "assistant":
        assistant.response_metadata = {"model_call_id": "foreign-model-call"}
    elif conflict == "nested":
        assistant.response_metadata = {"message_metadata": {"model_call_id": "foreign-model-call"}}
    elif conflict == "lc_id":
        assistant.id = "lc_run--foreign-model-call"
    elif conflict == "tool":
        result.response_metadata = {"model_call_id": "foreign-model-call"}
    else:
        result.tool_call_id = "call-foreign"
    response = ModelResponse(result=[assistant, result])

    with pytest.raises(ValueError, match="source-mismatch"):
        await dispatch(response, mode)
    assert assistant.response_metadata.get("model_call_id") != model_call_id
    assert result.response_metadata.get("model_call_id") != model_call_id


@pytest.mark.parametrize("prepared", [
    {"messages": "not-a-list", "tools": []},
    {"messages": [], "tools": "not-a-list"},
])
def test_prepare_projection_rejects_illegal_structure(bridge, prepared):
    bridge._prepare = MagicMock(return_value=prepared)
    with pytest.raises(TypeError, match="itemized provider projection 返回结构非法"):
        bridge._prepare_projection(object())


def test_prepare_projection_records_capability_loss(bridge, monkeypatch):
    bridge._prepare = MagicMock(return_value={"messages": [], "tools": [], "losses": ["image"], "include_structured_output": False})
    monkeypatch.setattr(model_call_execution, "_runtime_thread_id", lambda request: "thread-unit")
    monkeypatch.setattr(model_call_execution, "require_current_execution_admission", lambda **_: SimpleNamespace(job_id="job-unit"))
    warning = MagicMock()
    monkeypatch.setattr(model_call_execution.logger, "warning", warning)
    bridge._prepare_projection(object())
    loss_calls = [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]
    assert len(loss_calls) == 1
    assert loss_calls[0].args[1:] == ("session-unit", "job-unit", ["image"])


def test_prepare_projection_without_losses_skips_loss_identity(bridge, monkeypatch):
    bridge._prepare = MagicMock(return_value={"messages": [], "tools": [], "losses": (), "include_structured_output": False})

    def fail(**kwargs):
        raise AssertionError("losses 为空时不得解析 capability loss identity")

    monkeypatch.setattr(model_call_execution, "require_current_execution_admission", fail)
    warning = MagicMock()
    monkeypatch.setattr(model_call_execution.logger, "warning", warning)
    bridge._prepare_projection(object())
    assert not [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]


@pytest.fixture
def preparation_bridge(bridge):
    frozen = SimpleNamespace(activation_policy=SimpleNamespace(has_model_call_boundary=lambda: True))
    bridge._request_preparation = MagicMock(return_value=SimpleNamespace(checkpoint_ns="", creation_key="unit-creation", seal_key="unit-seal", admission=SimpleNamespace(session_id="unit-session", thread_id="unit-thread", execution_id="unit-execution", turn_id="unit-turn")))
    bridge._frozen_preparation = MagicMock(return_value=(None, frozen))
    bridge._resource_activation = SimpleNamespace(capture_model_call=AsyncMock(return_value=frozen), commit_model_call=AsyncMock(return_value=frozen))
    bridge._checkpointer = SimpleNamespace(prepare_model_call_boundary=MagicMock(return_value=(None, None)))
    bridge._complete_preparation = MagicMock(side_effect=lambda *_args: _args[-1])
    return bridge


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_actual_preparation_allocates_uuid7_for_frozen_call_and_callback(preparation_bridge, mode):
    bridge = preparation_bridge
    started = datetime.now(UTC)
    request = SimpleNamespace(messages=[])
    callback = bridge._prepare(request) if mode == "sync" else asyncio.run(bridge._aprepare(request))
    ended = datetime.now(UTC)
    identity = UUID(callback)
    assert identity.version == 7
    assert str(identity) == callback
    assert abs((uuid7_datetime_from_hex(identity.hex) - started).total_seconds()) < 1
    assert uuid7_datetime_from_hex(identity.hex) <= ended
    bridge._resource_activation.capture_model_call.assert_awaited_once()
    bridge._resource_activation.commit_model_call.assert_awaited_once_with(bridge._resource_activation.capture_model_call.return_value)
    assert bridge._resource_activation.capture_model_call.await_args.kwargs["model_call_id"] == callback
