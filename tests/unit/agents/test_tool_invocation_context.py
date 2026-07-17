from __future__ import annotations

import asyncio

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _request(tool_call_id: str):
    return type(
        "Request",
        (),
        {
            "tool_call": {
                "id": tool_call_id,
                "name": "test_tool",
                "args": {},
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
