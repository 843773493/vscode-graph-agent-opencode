from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.turn_execution_scope import ScopeCancelledError, TurnExecutionScope
from app.schemas.internal_v2.text_mutation import (
    TextMutationReceiptDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _request(
    tool_call_id: str, *, args: dict[str, object] | None = None,
    model_call_id: str | None = None,
) -> ToolCallRequest:
    call = {
        "id": tool_call_id, "name": "test_tool", "args": args or {}, "type": "tool_call",
    }
    messages = [] if model_call_id is None else [
        AIMessage(content="", id=f"lc_run--{model_call_id}", tool_calls=[call]),
    ]
    return ToolCallRequest(tool_call=call, tool=None, state={"messages": messages}, runtime=None)


@pytest.fixture
def invocation_failure_case():
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    async def fail(*_args, **_kwargs):
        commit_started.set()
        await release_commit.wait()

    sink = SimpleNamespace(
        prepare_tool_execution=AsyncMock(), fail_tool_invocation=AsyncMock(side_effect=fail),
    )
    return SimpleNamespace(sink=sink, commit_started=commit_started, release_commit=release_commit)


@pytest.mark.asyncio
async def test_tool_error_waits_for_original_result_commit_before_scheduler_failure(invocation_failure_case):
    case = invocation_failure_case
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)
    failure = RuntimeError("target-deleted")

    async def handler(_request_value):
        raise failure

    token = set_current_model_delta_sink(case.sink)
    try:
        task = asyncio.create_task(middleware.awrap_tool_call(
            _request("original-call", model_call_id="original-model"), handler,
        ))
        await asyncio.wait_for(case.commit_started.wait(), 1)
        assert not task.done()
        case.release_commit.set()
        with pytest.raises(RuntimeError, match="target-deleted") as raised:
            await task
        assert raised.value is failure
        case.sink.fail_tool_invocation.assert_awaited_once_with(
            "original-model", "original-call", error="target-deleted",
        )
        with pytest.raises(RuntimeError, match="缺少 tool_call_id"):
            context.require_tool_call_id()
    finally:
        case.release_commit.set()
        reset_current_model_delta_sink(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("local_timeout", [False, True])
async def test_file_cancellation_commits_unknown_result_before_scheduler_continues(invocation_failure_case, local_timeout):
    case = invocation_failure_case
    context = ToolInvocationContext(tool_timeout_seconds=0.03 if local_timeout else None)
    middleware = ToolInvocationContextMiddleware(context)
    entered = asyncio.Event()
    receipt = {"status": "error", "effect": "unknown", "code": "cancellation_requested", "retryable": False}

    async def handler(_request_value):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise asyncio.CancelledError(TextMutationReceiptDTO.model_validate(receipt)) from None

    token = set_current_model_delta_sink(case.sink)
    try:
        task = asyncio.create_task(middleware.awrap_tool_call(
            _request("file-call", model_call_id="file-model"), handler,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        if not local_timeout:
            task.cancel()
        await asyncio.wait_for(case.commit_started.wait(), 1)
        assert not task.done()
        if not local_timeout:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        case.release_commit.set()
        if local_timeout:
            result = await task
            assert isinstance(result, ToolMessage) and result.status == "error"
            assert json.loads(result.content) == TextMutationReceiptDTO.model_validate(receipt).model_dump(mode="json", exclude_none=True)
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        case.sink.fail_tool_invocation.assert_awaited_once()
        call = case.sink.fail_tool_invocation.await_args
        assert call.args == ("file-model", "file-call")
        if local_timeout:
            assert result.content == call.kwargs["error"]
        assert json.loads(call.kwargs["error"]) == TextMutationReceiptDTO.model_validate(receipt).model_dump(mode="json", exclude_none=True)
    finally:
        case.release_commit.set()
        reset_current_model_delta_sink(token)


@pytest.mark.asyncio
async def test_model_preparation_failure_never_claims_a_tool_result(invocation_failure_case):
    case = invocation_failure_case
    case.sink.prepare_tool_execution.side_effect = RuntimeError("原 ModelCall 登记失败")
    handler = AsyncMock()
    token = set_current_model_delta_sink(case.sink)
    try:
        with pytest.raises(RuntimeError, match="原 ModelCall 登记失败"):
            await ToolInvocationContextMiddleware(ToolInvocationContext()).awrap_tool_call(
                _request("original-call", model_call_id="original-model"), handler,
            )
        handler.assert_not_awaited()
        case.sink.fail_tool_invocation.assert_not_awaited()
    finally:
        reset_current_model_delta_sink(token)


@pytest.mark.asyncio
async def test_tool_invocation_context_is_isolated_for_parallel_calls():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)

    async def invoke(tool_call_id: str, model_call_id: str) -> str:
        async def handler(_request_value):
            before = context.require_tool_call_id()
            before_model = context.require_model_call_id()
            await asyncio.sleep(0)
            after = context.require_tool_call_id()
            assert before == after == tool_call_id
            assert before_model == context.require_model_call_id() == model_call_id
            return ToolMessage(content="ok", tool_call_id=tool_call_id)

        result = await middleware.awrap_tool_call(
            _request(tool_call_id, model_call_id=model_call_id),
            handler,
        )
        assert isinstance(result, ToolMessage)
        return result.tool_call_id

    assert await asyncio.gather(
        invoke("call_a", "00000000-0000-4000-8000-000000000001"),
        invoke("call_b", "00000000-0000-4000-8000-000000000002"),
    ) == [
        "call_a",
        "call_b",
    ]
    with pytest.raises(RuntimeError, match="缺少 tool_call_id"):
        context.require_tool_call_id()
    with pytest.raises(RuntimeError, match="model_call_id"):
        context.require_model_call_id()


@pytest.mark.asyncio
async def test_tool_child_scope_abort_does_not_leave_running_handler():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)
    scope = TurnExecutionScope("stream_1")
    started = asyncio.Event()

    async def handler(_request_value):
        started.set()
        await asyncio.Future()

    async def invoke():
        with pytest.raises(ScopeCancelledError, match="user_requested"):
            await middleware.awrap_tool_call(_request("call_1"), handler)

    from app.core.turn_execution_scope import (
        reset_current_turn_execution_scope,
        set_current_turn_execution_scope,
    )

    token = set_current_turn_execution_scope(scope)
    try:
        task = asyncio.create_task(invoke())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await scope.cancel("user_requested") is True
        await asyncio.wait_for(task, timeout=1)
    finally:
        reset_current_turn_execution_scope(token)
        await scope.close()


@pytest.mark.asyncio
async def test_tool_local_timeout_returns_error_without_cancelling_turn():
    context = ToolInvocationContext(tool_timeout_seconds=0.01)
    middleware = ToolInvocationContextMiddleware(context)
    scope = TurnExecutionScope("stream_1")

    async def handler(_request_value):
        await asyncio.sleep(1)
        return ToolMessage(content="late", tool_call_id="call_1")

    from app.core.turn_execution_scope import (
        reset_current_turn_execution_scope,
        set_current_turn_execution_scope,
    )

    token = set_current_turn_execution_scope(scope)
    try:
        result = await middleware.awrap_tool_call(_request("call_1"), handler)
    finally:
        reset_current_turn_execution_scope(token)
        await scope.close()

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "局部超时" in result.content
    assert scope.cancellation_signal.is_cancelled is False


@pytest.mark.asyncio
async def test_transport_timeout_returns_recoverable_tool_result_without_aborting_turn():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)
    scope = TurnExecutionScope("stream_transport_timeout")

    async def handler(_request_value):
        raise TimeoutError("timed out")

    from app.core.turn_execution_scope import (
        reset_current_turn_execution_scope,
        set_current_turn_execution_scope,
    )

    token = set_current_turn_execution_scope(scope)
    try:
        result = await middleware.awrap_tool_call(
            _request("call_transport_timeout"),
            handler,
        )
    finally:
        reset_current_turn_execution_scope(token)
        await scope.close()

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_transport_timeout"
    payload = json.loads(result.content)
    assert payload == {
        "status": "error",
        "code": "tool_execution_timeout",
        "error": (
            "工具执行超时: tool=test_tool, call_id=call_transport_timeout；"
            "下游操作结果未确认: timed out"
        ),
        "retryable": True,
        "recovery": "check_tool_state_before_retry",
    }
    assert scope.cancellation_signal.is_cancelled is False


@pytest.mark.asyncio
async def test_parallel_transport_timeouts_still_emit_one_result_per_tool_call():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)

    async def invoke(tool_call_id: str):
        async def handler(_request_value):
            raise TimeoutError("terminal manager timed out")

        return await middleware.awrap_tool_call(_request(tool_call_id), handler)

    results = await asyncio.gather(
        invoke("call_timeout_a"),
        invoke("call_timeout_b"),
        invoke("call_timeout_c"),
    )

    assert [result.tool_call_id for result in results] == [
        "call_timeout_a",
        "call_timeout_b",
        "call_timeout_c",
    ]
    assert all(result.status == "error" for result in results)
    assert all(
        json.loads(result.content)["code"] == "tool_execution_timeout"
        for result in results
    )


@pytest.mark.asyncio
async def test_tool_invocation_does_not_infer_resource_lease_from_public_arguments(
    tmp_path: Path,
) -> None:
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    # ToolInvocationContext 不再持有资源账本：通用调用链没有可注入的
    # lease 入口，公开参数（如 terminal_id）无法在通用层制造占用。
    assert not hasattr(ToolInvocationContext(), "resource_manager")
    middleware = ToolInvocationContextMiddleware(ToolInvocationContext())
    scope = TurnExecutionScope("stream_1")

    async def handler(_request_value):
        return ToolMessage(content="ok", tool_call_id="call_1")

    from app.core.turn_execution_scope import (
        reset_current_turn_execution_scope,
        set_current_turn_execution_scope,
    )

    token = set_current_turn_execution_scope(scope)
    try:
        result = await middleware.awrap_tool_call(
            _request("call_1", args={"terminal_id": "terminal_1"}),
            handler,
        )
    finally:
        reset_current_turn_execution_scope(token)
        await scope.close()

    assert isinstance(result, ToolMessage)
    assert ledger.get("terminal_1") is None
    assert ledger.leases_for_turn("stream_1") == []


def test_tool_invocation_context_rejects_missing_call_id():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)
    request = _request("")

    with pytest.raises(RuntimeError, match="工具调用缺少 tool_call_id"):
        middleware.wrap_tool_call(
            request,
            lambda _request_value: ToolMessage(
                content="不应执行",
                tool_call_id="missing",
            ),
        )


def test_thread_runtime_binding_carries_exact_trusted_identity():
    with pytest.raises(ValueError, match="session_id"):
        ThreadRuntimeBinding(session_id="   ", thread_id="main")
    with pytest.raises(ValueError, match="thread_id"):
        ThreadRuntimeBinding(session_id="ses_binding", thread_id="")

    binding = ThreadRuntimeBinding(
        session_id="ses_binding", thread_id="thr_00000000400070008000000000000002",
    )
    context = ToolInvocationContext(thread_binding=binding)

    assert context.thread_binding == binding
    # 没有 Agent runtime 绑定时不伪造归属，由调用方显式补齐。
    assert ToolInvocationContext().thread_binding is None


def test_agent_injects_call_id_without_exposing_runtime_parameter():
    context = ToolInvocationContext()
    observed_call_ids: list[str] = []

    @tool
    def context_aware_tool(value: str) -> str:
        """记录当前工具调用身份。"""
        observed_call_ids.append(context.require_tool_call_id())
        return value

    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_context",
                        "name": "context_aware_tool",
                        "args": {"value": "ok"},
                    }
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    agent = create_agent(
        model,
        tools=[context_aware_tool],
        middleware=[ToolInvocationContextMiddleware(context)],
    )

    result = agent.invoke({"messages": [HumanMessage(content="执行工具")]})

    assert observed_call_ids == ["call_context"]
    assert result["messages"][-1].content == "完成"
    assert context_aware_tool.args == {
        "value": {"title": "Value", "type": "string"}
    }
