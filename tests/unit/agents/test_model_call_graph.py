"""框架 model node 的闭包适配与同步/异步消息合并合同。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from app.agents.model_call_execution import DerivedModelResult
from app.agents.model_call_graph import create_model_call_agent, run_derived_agent
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.core.execution_pause import (
    ExecutionPause,
    bind_execution_pause,
    current_execution_pause,
)
from app.core.model_delta_context import (
    get_current_model_delta_sink,
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.execution_admission import DerivedExecutionReference


@dataclass
class FrameworkExecution:
    """只隔离封存业务，直接使用适配器绑定的真实模型与输出解析器。"""
    calls: list[tuple[str, ModelRequest]] = field(default_factory=list)

    @contextmanager
    def graph_request_scope(self):
        yield lambda: None

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


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_adopted_messages_replace_stale_graph_state(asynchronous):
    adopted = [HumanMessage(content="采用后的基准", id="adopted")]

    class AdoptingExecution(FrameworkExecution):
        adopted_messages = None

        @contextmanager
        def graph_request_scope(self):
            yield lambda: self.adopted_messages

        def execute(self, request):
            self.adopted_messages = adopted
            return ModelResponse(result=[AIMessage(content="新回答", id="response")])

        async def aexecute(self, request):
            self.adopted_messages = adopted
            return ModelResponse(result=[AIMessage(content="新回答", id="response")])

    execution = AdoptingExecution()
    graph = create_model_call_agent(
        FakeMessagesListChatModel(responses=[]), execution=execution,
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="过期基准", id="stale")]}) \
        if asynchronous else graph.invoke({"messages": [HumanMessage(content="过期基准", id="stale")]})

    assert [(message.id, message.content) for message in result["messages"]] == [
        ("adopted", "采用后的基准"), ("response", "新回答"),
    ]


class ToolCallingModel(FakeMessagesListChatModel):
    model_name: str = "graph-derived-model"
    invocation_purpose: str = "derived"
    def bind_tools(self, tools, **kwargs):
        return self


@dataclass(frozen=True)
class GraphDerivedRequest:
    """只提供图循环消费的字段，封存与准入由真实 owner 的集成测试覆盖。"""

    model: ToolCallingModel
    admission: SimpleNamespace
    reference: DerivedExecutionReference
    checkpoint_ns: str = ""
    model_call_attempt: int = 1
    plan: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(refs=(), contributions=()))
    request_only_content: dict[str, object] = field(default_factory=dict)


async def test_derived_graph_isolates_execution_and_enforces_tool_policy_through_convergence():
    pause = ExecutionPause()
    pause.request()
    parent_scope = TurnExecutionScope(turn_stream_id="derived-parent")
    parent_sink = SimpleNamespace(events=[])
    scope_token = set_current_turn_execution_scope(parent_scope)
    sink_token = set_current_model_delta_sink(parent_sink)

    async def run_case(first_call, tools, allowed_tool_ids, *, expected_error=None):
        events = []
        trace = []

        class TraceSink:
            async def append(self, session_id, event):
                assert session_id == "session-derived" and event.job_id == "job-derived"
                if event.type in {"job_completed", "job_failed", "job_cancelled"}:
                    assert events[-1][0] == "converge"
                trace.append(event)

        model = ToolCallingModel(responses=[])
        admission = SimpleNamespace(
            session_id="session-derived", thread_id="thread-derived",
            execution_id="execution-derived", job_id="job-derived",
        )
        reference = DerivedExecutionReference("task-derived", "plan-derived")
        initial = GraphDerivedRequest(model=model, admission=admission, reference=reference)

        def assert_isolated():
            assert current_execution_pause() is None
            assert get_current_model_delta_sink() is None
            assert get_current_turn_execution_scope() is None

        class Checkpointer:
            def converge_execution(self, *args, **kwargs):
                assert_isolated()
                events.append(("converge", kwargs["outcome"], kwargs["assembly_id"]))

        class Execution:
            _checkpointer = Checkpointer()

            @contextmanager
            def graph_request_scope(self):
                yield lambda: None

            def bind_model(self, *, bind_model, decode_output, name, structured_tools):
                assert structured_tools == {}

            def continue_derived_request(self, previous, result, request):
                assert_isolated()
                assert previous.model_call_attempt == 1
                assert result.dispatch_ref.assembly_id == "assembly-1"
                tool_message, = (message for message in request.messages if isinstance(message, ToolMessage))
                events.append(("continue", tuple(type(message).__name__ for message in request.messages),
                               tool_message.name, tool_message.tool_call_id, tool_message.status, tool_message.content))
                return initial

            async def aexecute_derived(self, request, *, publish=None):
                assert_isolated()
                call_number = sum(event[0] == "model" for event in events) + 1
                assert request.admission == admission and request.reference is reference
                assert request.model_call_attempt == call_number
                assert not any(event[0] == "converge" for event in events)
                events.append(("model", call_number))
                if call_number == 1:
                    response = ModelResponse(result=[AIMessage(content="", tool_calls=[first_call])])
                    refs = ()
                else:
                    response = ModelResponse(result=[AIMessage(content="完整回答")])
                    refs = (SimpleNamespace(ref_id="derived-final-item"),)
                return DerivedModelResult(
                    response=response,
                    dispatch_ref=SimpleNamespace(assembly_id=f"assembly-{call_number}"),
                    output_refs=refs,
                )

        try:
            with bind_execution_pause(pause):
                result = await asyncio.wait_for(run_derived_agent(
                initial, execution=Execution(), max_model_calls=2,
                tools=tools, allowed_tool_ids=allowed_tool_ids,
                trace_sink=TraceSink(),
            ), timeout=3)
            if expected_error is not None:
                raise AssertionError(f"派生执行应当失败并包含 {expected_error!r}")
            assert result.response.result[0].text == "完整回答"
            return events
        except TypeError as error:
            if expected_error is None:
                raise
            assert expected_error in str(error)
            return events
        finally:
            assert [event.type for event in trace] == [
                "job_created", "agent_start", "job_failed" if expected_error else "job_completed",
            ]
            assert trace[0].payload.thread_id == admission.thread_id
            assert trace[0].payload.message_metadata == {
                "execution_id": admission.execution_id,
                "task_id": reference.task_id, "plan_id": reference.plan_id, "checkpoint_ns": "",
                "model": "graph-derived-model", "source_cutoff": None,
                "summary_selection": "no_applicable_ready",
            }
            assert current_execution_pause() is None and pause.state == "paused"
            assert get_current_model_delta_sink() is parent_sink
            assert get_current_turn_execution_scope() is parent_scope

    async def read_note():
        assert current_execution_pause() is None
        assert get_current_model_delta_sink() is None
        assert get_current_turn_execution_scope() is None
        return "note"

    async def command_tool():
        return Command(update={"messages": [ToolMessage(
            content="不应修改派生图", name="read_note", tool_call_id="derived-command",
        )]})

    async def invoke_extension(tool_name: str, arguments: dict[str, object]):
        raise AssertionError(f"未授权扩展工具不应执行: {tool_name}")

    read_tool = StructuredTool.from_function(coroutine=read_note, name="read_note", description="读取测试资料")
    command = StructuredTool.from_function(coroutine=command_tool, name="read_note", description="返回Graph命令")
    envelope = StructuredTool.from_function(
        coroutine=invoke_extension, name=EXTENSION_TOOL_INVOKER_NAME, description="调用扩展工具",
    )
    try:
        allowed = await run_case(
            {"name": "read_note", "args": {}, "id": "derived-read"},
            [read_tool], ("read_note",),
        )
        denied = await run_case(
            {"name": "read_note", "args": {}, "id": "derived-denied"},
            [read_tool], (),
        )
        envelope_denied = await run_case(
            {"name": EXTENSION_TOOL_INVOKER_NAME,
             "args": {"tool_name": "read_note", "arguments": {}}, "id": "derived-envelope"},
            [envelope], (EXTENSION_TOOL_INVOKER_NAME,),
        )
        command_blocked = await run_case(
            {"name": "read_note", "args": {}, "id": "derived-command"},
            [command], ("read_note",), expected_error="derived-tool-command-unsupported",
        )
    finally:
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        pause.resume()
        await parent_scope.close()

    assert [event[0] for event in allowed] == ["model", "continue", "model", "converge"]
    assert allowed[1][1] == ("AIMessage", "ToolMessage")
    assert allowed[-1] == ("converge", "completed", "assembly-2")
    for events, tool_name, call_id in (
        (denied, "read_note", "derived-denied"),
        (envelope_denied, EXTENSION_TOOL_INVOKER_NAME, "derived-envelope"),
    ):
        assert [event[0] for event in events] == ["model", "continue", "model", "converge"]
        assert events[1][2:5] == (tool_name, call_id, "error")
        assert "derived-tool-denied" in events[1][5]
        assert events[-1][1] == "completed"
    assert [event[0] for event in command_blocked] == ["model", "converge"]
    assert command_blocked[-1] == ("converge", "failed", "assembly-1")
    assert parent_sink.events == []


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
