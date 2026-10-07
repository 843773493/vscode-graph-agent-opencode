from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from app.agents import itemized_context_middleware
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)


@pytest.fixture
def bridge(registered_tool_set_producer_factory):
    return SealedAssemblyDispatchBridge(
        checkpointer=object(), session_id="session-unit", thread_id="thread-unit",
        context_source_manager=ContextSourceManager(),
        tool_set_producer=registered_tool_set_producer_factory(),
        resource_activation=None,
    )


@pytest.fixture
def response_dispatch(bridge, monkeypatch):
    model_call_id = "00000000-0000-4000-8000-000000000001"
    prepared = {"snapshot": object(), "callback_model_call_id": model_call_id}
    bridge._prepare_projection = MagicMock(return_value=(prepared, [], []))
    bridge._aprepare = AsyncMock(return_value=prepared)
    bridge._project_prepared = MagicMock(return_value=(prepared, [], []))
    monkeypatch.setattr(
        itemized_context_middleware, "sealed_assembly_dispatch_scope",
        lambda *_args, **_kwargs: nullcontext(),
    )

    async def dispatch(response, mode):
        request = ModelRequest(model=MagicMock(), messages=[])
        if mode == "sync":
            return bridge.wrap_model_call(request, lambda _request: response)

        async def handler(_request):
            return response

        return await bridge.awrap_model_call(request, handler)

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
    monkeypatch.setattr(itemized_context_middleware, "_runtime_thread_id", lambda request: "thread-unit")
    monkeypatch.setattr(itemized_context_middleware, "require_current_execution_admission", lambda **_: SimpleNamespace(job_id="job-unit"))
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)
    bridge._prepare_projection(object())
    loss_calls = [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]
    assert len(loss_calls) == 1
    assert loss_calls[0].args[1:] == ("session-unit", "job-unit", ["image"])


def test_prepare_projection_without_losses_skips_loss_identity(bridge, monkeypatch):
    bridge._prepare = MagicMock(return_value={"messages": [], "tools": [], "losses": (), "include_structured_output": False})

    def fail(**kwargs):
        raise AssertionError("losses 为空时不得解析 capability loss identity")

    monkeypatch.setattr(itemized_context_middleware, "require_current_execution_admission", fail)
    warning = MagicMock()
    monkeypatch.setattr(itemized_context_middleware.logger, "warning", warning)
    bridge._prepare_projection(object())
    assert not [call for call in warning.call_args_list if call.args[0].startswith("itemized provider projection capability loss")]
