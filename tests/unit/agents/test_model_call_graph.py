"""框架 model node 的闭包适配与同步/异步消息合并合同。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from app.agents.model_call_graph import create_model_call_agent
from app.agents.tool_invocation_context import ToolInvocationContext, ToolInvocationContextMiddleware
from app.core.execution_pause import ExecutionPause, bind_execution_pause
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)


@dataclass
class FrameworkExecution:
    """只隔离封存业务，直接使用适配器绑定的真实模型与输出解析器。"""
    calls: list[tuple[str, ModelRequest]] = field(default_factory=list)

    def bind_model(self, *, bind_model, decode_output, name, structured_tools):
        self.bind = bind_model
        self.decode = decode_output
        assert structured_tools == {}

    def execute(self, request):
        self.calls.append(("sync", request))
        bound, response_format = self.bind(request)
        output = bound.invoke(request.messages)
        return ModelResponse(result=self.decode(output, response_format)["messages"])

    async def aexecute(self, request):
        self.calls.append(("async", request))
        bound, response_format = self.bind(request)
        output = await bound.ainvoke(request.messages)
        return ModelResponse(result=self.decode(output, response_format)["messages"])


@pytest.fixture
def model_graph():
    executor = FrameworkExecution()
    model = FakeMessagesListChatModel(responses=[AIMessage(content="框架响应", id="assistant")])
    return create_model_call_agent(model, execution=executor), executor


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_adapter_executes_and_merges_messages(model_graph, asynchronous):
    graph, executor = model_graph
    user = HumanMessage(content="框架输入", id="user")
    inputs = {"messages": [user]}
    result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)
    assert [(message.id, message.content) for message in result["messages"]] == [
        ("user", "框架输入"), ("assistant", "框架响应"),
    ]
    assert [kind for kind, request in executor.calls] == ["async" if asynchronous else "sync"]
    assert executor.calls[0][1].messages == [user]


class ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def media_graph_factory():
    def create(form: str, status: str):
        content = [{"type": "text", "text": "媒体结果"},
                   {"type": "audio", "base64": "YXVkaW8=", "mime_type": "audio/wav"}]
        artifact = {"structured_content": {"status": status}}

        def result():
            message = ToolMessage(content=content, artifact=artifact, status=status,
                                  tool_call_id="media-call", name="media")
            if form == "command":
                return Command(update={"messages": [message]})
            if form == "list":
                return [Command(update={}), message]
            return message

        async def aresult():
            return result()

        target = StructuredTool.from_function(func=result, coroutine=aresult, name="media", description="媒体工具")
        model = ToolCallingModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "media", "args": {}, "id": "media-call"}]),
            AIMessage(content="完成"),
        ])
        executor = FrameworkExecution()
        return create_model_call_agent(model, execution=executor, tools=[target]), content, artifact
    return create


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("form", ["message", "command", "list"])
@pytest.mark.parametrize("status", ["success", "error"])
async def test_graph_preserves_media_and_artifact_in_all_tool_result_forms(
    media_graph_factory, asynchronous, form, status,
):
    graph, content, artifact = media_graph_factory(form, status)
    inputs = {"messages": [HumanMessage(content="读取媒体")]}
    result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)
    messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(messages) == 1
    assert messages[0].content == content
    assert messages[0].artifact == artifact
    assert messages[0].status == status
    assert messages[0].tool_call_id == "media-call"


@pytest.fixture
def paused_tool_graph():
    pause = ExecutionPause()
    model_finished = asyncio.Event()
    tool_started = asyncio.Event()

    async def execute_tool():
        scope = get_current_turn_execution_scope()
        assert scope is not None and scope.deadline is not None
        scope.raise_if_cancelled()
        tool_started.set()
        return "结果"

    class PausingExecution(FrameworkExecution):
        async def aexecute(self, request):
            result = await super().aexecute(request)
            if len(self.calls) == 1:
                pause.request()
                model_finished.set()
            return result

    executor = PausingExecution()
    tool = StructuredTool.from_function(
        coroutine=execute_tool, name="probe", description="暂停探针",
    )
    model = ToolCallingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "probe", "args": {}, "id": "probe-call"}]),
        AIMessage(content="完成"),
    ])
    graph = create_model_call_agent(
        model, execution=executor, tools=[tool], middleware=[
            ToolInvocationContextMiddleware(ToolInvocationContext(tool_timeout_seconds=0.02)),
        ],
    )
    return graph, executor, pause, model_finished, tool_started


@pytest.fixture
async def graph_turn_scope():
    scope = TurnExecutionScope(turn_stream_id="paused-model-graph")
    try:
        yield scope
    finally:
        await scope.close()


async def test_pause_defers_tool_scope_timeout_and_preserves_graph(paused_tool_graph, graph_turn_scope):
    graph, executor, pause, model_finished, tool_started = paused_tool_graph
    token = set_current_turn_execution_scope(graph_turn_scope)
    try:
        with bind_execution_pause(pause):
            execution = asyncio.create_task(graph.ainvoke({"messages": [HumanMessage(content="执行探针")]}))
    finally:
        reset_current_turn_execution_scope(token)
    try:
        await model_finished.wait()
        await asyncio.sleep(0.06)
        assert pause.state == "paused"
        assert not tool_started.is_set()
        assert not execution.done()
        pause.resume()
        result = await execution
        assert tool_started.is_set()
        assert len(executor.calls) == 2
        assert [message.content for message in result["messages"] if isinstance(message, AIMessage)] == ["", "完成"]
        tools = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        assert len(tools) == 1 and tools[0].status == "success" and tools[0].content == "结果"
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


async def test_cancellation_drains_original_paused_graph(paused_tool_graph):
    graph, _executor, pause, model_finished, tool_started = paused_tool_graph
    with bind_execution_pause(pause):
        execution = asyncio.create_task(graph.ainvoke({"messages": [HumanMessage(content="执行探针")]}))
    await model_finished.wait()
    await asyncio.sleep(0.01)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert not tool_started.is_set()
    assert pause.active_count == 0
