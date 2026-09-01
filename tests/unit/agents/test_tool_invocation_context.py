from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.core.turn_execution_scope import ScopeCancelledError, TurnExecutionScope
from app.services.infrastructure.resource_manager import ResourceManager


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _request(tool_call_id: str, *, args: dict[str, object] | None = None):
    return type(
        "Request",
        (),
        {
            "tool_call": {
                "id": tool_call_id,
                "name": "test_tool",
                "args": args or {},
            }
        },
    )()


@pytest.mark.asyncio
async def test_tool_invocation_context_is_isolated_for_parallel_calls():
    context = ToolInvocationContext()
    middleware = ToolInvocationContextMiddleware(context)

    async def invoke(tool_call_id: str) -> str:
        async def handler(_request_value):
            before = context.require_tool_call_id()
            await asyncio.sleep(0)
            after = context.require_tool_call_id()
            assert before == after == tool_call_id
            return ToolMessage(content="ok", tool_call_id=tool_call_id)

        result = await middleware.awrap_tool_call(
            _request(tool_call_id),
            handler,
        )
        assert isinstance(result, ToolMessage)
        return result.tool_call_id

    assert await asyncio.gather(invoke("call_a"), invoke("call_b")) == [
        "call_a",
        "call_b",
    ]
    with pytest.raises(RuntimeError, match="缺少 tool_call_id"):
        context.require_tool_call_id()


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
async def test_tool_resource_reference_gets_lease_and_releases_on_normal_completion(
    tmp_path: Path,
) -> None:
    manager = ResourceManager(state_path=tmp_path / "resources.json")
    context = ToolInvocationContext(resource_manager=manager)
    middleware = ToolInvocationContextMiddleware(context)
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
    assert manager.get("terminal_1") is not None
    assert manager.leases_for_turn("stream_1")[0].status == "released"


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
