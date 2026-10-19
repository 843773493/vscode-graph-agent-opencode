from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.runnables.config import ensure_config, var_child_runnable_config
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.session_messaging import create_send_message_to_session_tool
from app.core.job_context import get_active_tool_name, get_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.lifecycle import LifetimeScope
from app.core.model_delta_context import (
    AgentRunLifecycleCallbackHandler,
    model_tool_invocation_scope,
)
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    CancellationSignal,
    ScopeCancelledError,
    TurnExecutionScope,
    get_current_turn_execution_scope,
)
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.schemas.event import ModelTokenUsagePayload
from app.services.orchestration.event_stream.contracts import (
    AgentEventStreamTimeoutError,
)
from app.services.orchestration.event_stream.model_events import (
    last_model_token_usage,
)
from app.services.orchestration.event_stream.processor import (
    process_agent_event_stream,
)
from app.services.orchestration.event_stream.tool_events import (
    activity_result_detail,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from app.services.orchestration.trace_observer import MessageStreamTraceObserver

pytestmark = pytest.mark.usefixtures("workspace_scope")


@pytest.fixture
async def cancellation_signal() -> AsyncIterator[CancellationSignal]:
    scope = LifetimeScope("event-stream-cancellation")
    try:
        yield CancellationSignal(lifetime_scope=scope)
    finally:
        await scope.close()


class FakeAgent:
    def __init__(
        self, events: list[dict[str, Any]], *,
        tool_invocations: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._events = events
        self.tool_invocations = tool_invocations or {}
        self.received_configs: list[dict[str, Any]] = []

    async def astream_events(
        self,
        input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del input_payload, version
        self.received_configs.append(config)
        config_metadata = config.get("metadata")
        if not isinstance(config_metadata, dict):
            raise TypeError("测试 Agent 必须收到 dict 类型 metadata")
        callbacks: list[asyncio.Task[None]] = []
        try:
            for event in self._events:
                event_metadata = event.get("metadata", {})
                if not isinstance(event_metadata, dict):
                    raise TypeError("测试事件 metadata 必须为 dict")
                callback = None
                if event["event"] == "on_tool_start" and event["run_id"] in self.tool_invocations:
                    model_call_id, provider_id = self.tool_invocations[event["run_id"]]
                    lifecycle = next(
                        handler for handler in config["callbacks"]
                        if isinstance(handler, AgentRunLifecycleCallbackHandler)
                    )
                    with model_tool_invocation_scope(model_call_id, provider_id):
                        callback = asyncio.create_task(lifecycle.on_tool_start(
                            {}, "", run_id=event["run_id"], tool_call_id=provider_id,
                        ))
                    callbacks.append(callback)
                yield {
                    **event,
                    "metadata": {**config_metadata, **event_metadata},
                }
                if callback is not None:
                    await callback
        finally:
            for callback in callbacks:
                if not callback.done():
                    callback.cancel()
            await asyncio.gather(*callbacks, return_exceptions=True)


class GraphFakeAgent(FakeAgent):
    def __init__(
        self, events: list[dict[str, Any]], tools: list[BaseTool], *,
        tool_invocations: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(events, tool_invocations=tool_invocations)
        self._tools_by_name = {item.name: item for item in tools}

    def get_graph(self) -> SimpleNamespace:
        return SimpleNamespace(
            nodes={
                "tools": SimpleNamespace(
                    data=SimpleNamespace(tools_by_name=self._tools_by_name),
                )
            }
        )


class DelayedFakeAgent(FakeAgent):
    def __init__(self, steps: list[tuple[float, dict[str, Any]]]) -> None:
        super().__init__([event for _delay, event in steps])
        self.delays = [delay for delay, _event in steps]
        self.emitted_events = 0
        self.closed = False

    async def astream_events(self, *args, **kwargs) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in super().astream_events(*args, **kwargs):
                await asyncio.sleep(self.delays[self.emitted_events])
                self.emitted_events += 1
                yield event
        finally:
            self.closed = True


class ResolvingConfigAgent(FakeAgent):
    def __init__(self) -> None:
        super().__init__([])
        self.resolved_configs: list[dict[str, Any]] = []

    async def astream_events(
        self,
        input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del input_payload, version
        self.received_configs.append(config)
        self.resolved_configs.append(ensure_config(config))
        if False:
            yield {}


class HangingBeforeProgressAgent:
    async def astream_events(
        self,
        _input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del config, version
        await asyncio.Future()
        if False:
            yield {}


class LifecycleEventsBeforeModelAgent:
    async def astream_events(
        self,
        _input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del version
        metadata = config.get("metadata")
        if not isinstance(metadata, dict):
            raise TypeError("测试 Agent 必须收到 dict 类型 metadata")
        event_base = {
            "name": "agent",
            "metadata": metadata,
        }
        yield {"event": "on_chain_start", "data": {}, **event_base}
        await asyncio.sleep(0.02)
        yield {"event": "on_prompt_start", "data": {}, **event_base}
        await asyncio.sleep(0.02)
        yield {
            "event": "on_chat_model_start",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {},
            "metadata": metadata,
        }
        yield {
            "event": "on_chat_model_stream",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "text",
                            "text": "lifecycle-progress",
                            "id": "lifecycle-progress",
                            "index": 0,
                        }
                    ]
                )
            },
            "metadata": metadata,
        }
        yield {
            "event": "on_chat_model_end",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {},
            "metadata": metadata,
        }


class CancellationSensitiveAgent:
    async def astream_events(
        self,
        _input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del config, version
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            # 旧的 wait_for 实现会把自己的超时取消直接暴露成这个结果，
            # 让上层错误地把工具分派超时记录成 execution_cancelled。
            raise asyncio.CancelledError("execution_cancelled") from error
        if False:
            yield {}


class SlowlyStreamingModelAgent:
    async def astream_events(
        self,
        _input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del version
        config_metadata = config.get("metadata")
        if not isinstance(config_metadata, dict):
            raise TypeError("测试 Agent 必须收到 dict 类型 metadata")
        event_base = {
            "name": "BoxteamOpenAIResponsesModel",
            "metadata": config_metadata,
        }
        yield {"event": "on_chat_model_start", "data": {}, **event_base}
        for index in range(2):
            await asyncio.sleep(0.025)
            yield {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": AIMessageChunk(
                        content=[
                            {
                                "type": "text",
                                "text": f"part-{index}",
                                "id": f"part-{index}",
                                "index": 0,
                            }
                        ]
                    )
                },
                **event_base,
            }
        await asyncio.sleep(0.025)
        yield {"event": "on_chat_model_end", "data": {}, **event_base}


class FakeSessionChangesService:
    def capture_before(self, file_path: str) -> object:
        raise AssertionError(f"本测试不应捕获文件快照: {file_path}")

    async def record_tool_file_edit(self, **kwargs: Any) -> object:
        raise AssertionError(f"本测试不应记录文件修改: {kwargs}")


class RecordingSessionChangesService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def capture_before(self, file_path: str) -> object:
        return {"file_path": file_path}

    async def record_tool_file_edit(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


@pytest.mark.asyncio
async def test_agent_stream_fails_when_no_model_or_tool_event_arrives(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentEventStreamTimeoutError, match="首个模型/工具事件"):
        await process_agent_event_stream(
            agent=HangingBeforeProgressAgent(),
            input_payload={"messages": []},
            config={},
            session_id="ses_first_event_timeout",
            job_id="job_first_event_timeout",
            turn_id="job_first_event_timeout",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=FakeSessionChangesService(),
            workspace_root=tmp_path,
            model_timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_lifecycle_events_extend_initial_model_event_watchdog(
    tmp_path: Path,
) -> None:
    result = await process_agent_event_stream(
        agent=LifecycleEventsBeforeModelAgent(),
        input_payload={"messages": []},
        config={},
        session_id="ses_lifecycle_event_watchdog",
        job_id="job_lifecycle_event_watchdog",
        turn_id="job_lifecycle_event_watchdog",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        model_timeout_seconds=0.03,
    )

    assert result.final_text == "lifecycle-progress"


@pytest.mark.asyncio
async def test_active_model_events_refresh_idle_watchdog_for_long_response(
    tmp_path: Path,
) -> None:
    result = await process_agent_event_stream(
        agent=SlowlyStreamingModelAgent(),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_idle_watchdog",
        job_id="job_model_idle_watchdog",
        turn_id="job_model_idle_watchdog",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        model_timeout_seconds=0.04,
    )

    assert result.final_text == "part-0part-1"


@pytest.mark.asyncio
async def test_default_model_wait_after_start_uses_job_budget_not_idle_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ModelStartsThenWaitsAgent:
        async def astream_events(
            self,
            _input_payload: dict[str, Any],
            *,
            config: dict[str, Any],
            version: str,
        ) -> AsyncIterator[dict[str, Any]]:
            del version
            metadata = config.get("metadata")
            if not isinstance(metadata, dict):
                raise TypeError("测试 Agent 必须收到 dict 类型 metadata")
            event_base = {
                "name": "BoxteamOpenAIResponsesModel",
                "metadata": metadata,
            }
            yield {"event": "on_chat_model_start", "data": {}, **event_base}
            await asyncio.sleep(0.03)
            yield {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": AIMessageChunk(
                        content=[
                            {
                                "type": "text",
                                "text": "after-wait",
                                "id": "after-wait",
                                "index": 0,
                            }
                        ]
                    )
                },
                **event_base,
            }
            yield {"event": "on_chat_model_end", "data": {}, **event_base}

    monkeypatch.setattr(
        "app.services.orchestration.event_stream.reader.DEFAULT_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.01,
    )
    result = await process_agent_event_stream(
        agent=ModelStartsThenWaitsAgent(),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_wait_after_start",
        job_id="job_model_wait_after_start",
        turn_id="job_model_wait_after_start",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
    )

    assert result.final_text == "after-wait"


@pytest.mark.asyncio
async def test_active_model_stream_is_not_cancelled_by_fixed_model_scope_deadline(
    tmp_path: Path,
) -> None:
    class ActiveStreamingAgent:
        async def astream_events(
            self,
            _input_payload: dict[str, Any],
            *,
            config: dict[str, Any],
            version: str,
        ) -> AsyncIterator[dict[str, Any]]:
            del version
            metadata = config.get("metadata")
            if not isinstance(metadata, dict):
                raise TypeError("测试 Agent 必须收到 dict 类型 metadata")
            event_base = {
                "name": "BoxteamOpenAIResponsesModel",
                "metadata": metadata,
            }
            yield {"event": "on_chat_model_start", "data": {}, **event_base}
            for index in range(2):
                await asyncio.sleep(0.03)
                scope = get_current_turn_execution_scope()
                if scope is not None:
                    scope.raise_if_cancelled()
                yield {
                    "event": "on_chat_model_stream",
                    "data": {
                        "chunk": AIMessageChunk(
                            content=[
                                {
                                    "type": "text",
                                    "text": f"part-{index}",
                                    "id": f"active-part-{index}",
                                    "index": 0,
                                }
                            ]
                        )
                    },
                    **event_base,
                }
            await asyncio.sleep(0.03)
            scope = get_current_turn_execution_scope()
            if scope is not None:
                scope.raise_if_cancelled()
            yield {"event": "on_chat_model_end", "data": {}, **event_base}

    execution_scope = TurnExecutionScope("stream_active_model_scope")
    try:
        result = await process_agent_event_stream(
            agent=ActiveStreamingAgent(),
            input_payload={"messages": []},
            config={},
            session_id="ses_active_model_scope",
            job_id="job_active_model_scope",
            turn_id="job_active_model_scope",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=FakeSessionChangesService(),
            workspace_root=tmp_path,
            execution_scope=execution_scope,
            model_timeout_seconds=0.05,
        )
    finally:
        await execution_scope.close()

    assert result.final_text == "part-0part-1"


@pytest.fixture
async def dispatch_watchdog_case(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
):
    count = getattr(request, "param", 3)
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_dispatch_progress")
    sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=sink,
        tool_result_sink=tool_result_sink,
    )
    await runtime.start_model("model_dispatch_progress", "primary")
    arguments = [{"path": f"fixture-{index}.txt"} for index in range(count)]
    provider_ids = [f"call_{index}" for index in range(count)]
    await runtime.accept_message_chunk(AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"index": index, "id": provider_ids[index], "name": "read_file",
             "args": json.dumps(args)}
            for index, args in enumerate(arguments)
        ],
    ))
    await runtime.finish_model()
    writer.commit.reset_mock()
    sink.reset_mock()
    tool_result_sink.reset_mock()
    starts = [
        {"event": "on_tool_start", "run_id": f"run_{index}", "name": "read_file",
         "data": {"input": args}, "metadata": {}}
        for index, args in enumerate(arguments)
    ]
    ends = [
        {"event": "on_tool_end", "run_id": f"run_{index}", "name": "read_file",
         "data": {"output": ToolMessage(
             content=f"result-{index}", tool_call_id=provider_ids[index], name="read_file",
         )}, "metadata": {}}
        for index in range(count)
    ]

    async def process(agent, *, timeout=0.05, publish=None, cancellation_signal=None):
        session_id = "ses_dispatch_progress"
        if isinstance(agent, FakeAgent):
            agent.tool_invocations.update({
                f"run_{index}": ("model_dispatch_progress", provider_id)
                for index, provider_id in enumerate(provider_ids)
            })
        try:
            return await process_agent_event_stream(
                agent=agent, input_payload={"messages": []}, config={},
                session_id=session_id, job_id="job_dispatch_progress",
                turn_id="job_dispatch_progress", agent_id="default",
                custom_tool_skill_sources={},
                publish=publish or (lambda *_args, **_kwargs: _async_noop()),
                session_changes_service=session_changes_service, workspace_root=tmp_path,
                message_stream_runtime=runtime, tool_dispatch_timeout_seconds=timeout,
                cancellation_signal=cancellation_signal,
            )
        finally:
            SessionInterruptState.clear(session_id)

    return SimpleNamespace(
        runtime=runtime, writer=writer, sink=sink,
        tool_result_sink=tool_result_sink, starts=starts, ends=ends,
        call_ids=[f"model_dispatch_progress:tool-call:{call_id}" for call_id in provider_ids],
        process=process,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_watchdog_case", [130], indirect=True)
async def test_tool_dispatch_progress_refreshes_wait_for_full_parallel_batch(dispatch_watchdog_case):
    case = dispatch_watchdog_case
    # 总分派时间大于预算，但每个 canonical call 都持续被真实 processor claim。
    agent = DelayedFakeAgent([
        *((0.002, event) for event in case.starts),
        *((0, event) for event in reversed(case.ends)),
    ])
    await case.process(agent)

    started = [call.args[1] for call in case.writer.commit.await_args_list
               if call.args[0] == "tool.started"]
    completed = [call.args[1] for call in case.writer.commit.await_args_list
                 if call.args[0] == "tool.completed"]
    assert [event["tool_call_id"] for event in started] == case.call_ids
    assert [event["tool_call_id"] for event in completed] == list(reversed(case.call_ids))
    assert all(event["outcome"] == "success" for event in completed)
    assert case.runtime.pending_tool_calls() == ()
    assert case.runtime._active_tool_executions == {}
    completions: list[ToolResultCompletion] = [
        call.args[0] for call in case.tool_result_sink.await_args_list
    ]
    assert [completion.tool_call_id for completion in completions] == list(reversed(case.call_ids))
    assert [completion.tool_attempt_id for completion in completions] == [
        f"run_{index}" for index in reversed(range(len(case.call_ids)))
    ]
    assert all(completion.model_call_id == "model_dispatch_progress" for completion in completions)
    assert all(completion.tool_name == "read_file" for completion in completions)
    assert [completion.tool_invocation_id for completion in completions] == [
        f"tool-invocation:model_dispatch_progress:call_{index}"
        for index in reversed(range(len(case.call_ids)))
    ]
    assert [completion.content for completion in completions] == [
        f"result-{index}" for index in reversed(range(len(case.call_ids)))
    ]
    assert all(completion.tool_outcome == "success" for completion in completions)
    assert agent.closed


@pytest.mark.asyncio
async def test_buffered_dispatch_excludes_processing_time_of_completed_tool(dispatch_watchdog_case):
    case = dispatch_watchdog_case

    async def publish(event_type, payload):
        if event_type == EventType.TOOL_CALL_END and payload["part_id"] == "run_0":
            # 下一 start 已在事件源中，慢的是当前结果的提交/发布消费者。
            await asyncio.sleep(0.08)

    events = [event for pair in zip(case.starts, case.ends, strict=True) for event in pair]
    await case.process(FakeAgent(events), timeout=0.03, publish=publish)
    assert case.runtime.pending_tool_calls() == ()
    assert len(case.runtime._completed_tool_result_call_ids) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_count", [0, 2])
async def test_chain_noise_cannot_refresh_stalled_dispatch(dispatch_watchdog_case, claimed_count):
    case = dispatch_watchdog_case
    noise = {"event": "on_chain_stream", "name": "agent", "data": {}, "metadata": {}}
    agent = DelayedFakeAgent([
        *((0.015, event) for event in case.starts[:claimed_count]),
        *((0.01, noise) for _ in range(12)),
    ])
    with pytest.raises(AgentEventStreamTimeoutError) as raised:
        await case.process(agent)
    assert raised.value.code == "tool_dispatch_timeout"
    assert agent.emitted_events < claimed_count + 12
    assert agent.closed
    incomplete = [call.args[1] for call in case.writer.commit.await_args_list
                  if call.args[0] == "tool_call.completed" and call.args[1]["status"] == "incomplete"]
    assert [event["tool_call_id"] for event in incomplete] == case.call_ids[claimed_count:]
    assert all(event["completion_reason"] == "tool_dispatch_timeout" for event in incomplete)
    assert set(case.runtime._active_tool_executions) == {f"run_{index}" for index in range(claimed_count)}
    assert not [call for call in case.writer.commit.await_args_list if call.args[0] == "tool.completed"]


@pytest.mark.asyncio
async def test_dispatch_budget_does_not_limit_already_claimed_tool_execution(dispatch_watchdog_case):
    case = dispatch_watchdog_case
    agent = DelayedFakeAgent([
        *((0, event) for event in case.starts),
        (0.08, case.ends[0]),
        *((0, event) for event in case.ends[1:]),
    ])
    await case.process(agent, timeout=0.03)
    assert len(case.runtime._completed_tool_result_call_ids) == 3


@pytest.mark.asyncio
async def test_tool_message_without_real_start_remains_pending_until_dispatch_timeout(
    dispatch_watchdog_case,
):
    case = dispatch_watchdog_case

    noise = {"event": "on_chain_stream", "name": "agent", "data": {}, "metadata": {}}
    agent = DelayedFakeAgent([(0.06, noise)])
    reconcile = asyncio.create_task(
        case.runtime.complete_tool_from_message(
            case.ends[0]["data"]["output"],
            model_call_id="model_dispatch_progress",
        )
    )
    await asyncio.sleep(0)
    assert not reconcile.done()
    try:
        with pytest.raises(AgentEventStreamTimeoutError) as raised:
            await case.process(agent, timeout=0.03)
        assert raised.value.code == "tool_dispatch_timeout"
    finally:
        if not reconcile.done():
            reconcile.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reconcile

    assert agent.closed
    assert case.runtime.pending_tool_calls() == ()
    assert case.runtime._active_tool_executions == {}
    assert case.runtime._completed_tool_result_call_ids == set()
    assert case.tool_result_sink.await_count == 0
    assert not [
        call for call in case.writer.commit.await_args_list
        if call.args[0] in {"tool.started", "tool.completed"}
    ]
    incomplete = [
        call.args[1]
        for call in case.writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    ]
    assert [event["tool_call_id"] for event in incomplete] == case.call_ids
    assert all(event["status"] == "incomplete" for event in incomplete)
    assert all(event["completion_reason"] == "tool_dispatch_timeout" for event in incomplete)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_watchdog_case", [0], indirect=True)
async def test_default_model_wait_does_not_arm_dispatch_for_streaming_tool_arguments(dispatch_watchdog_case):
    case = dispatch_watchdog_case
    model_base = {"name": "BoxteamOpenAIResponsesModel", "run_id": "model_streaming_args", "metadata": {}}
    fragments = [
        AIMessageChunk(content="", tool_call_chunks=[
            {"index": 0, "id": "call_streaming_args", "name": "read_file", "args": '{"path":'},
        ]),
        AIMessageChunk(content="", tool_call_chunks=[
            {"index": 0, "args": '"fixture.txt"}'},
        ]),
    ]

    class StreamingArgumentsAgent(DelayedFakeAgent):
        async def astream_events(self, *args, **kwargs):
            async for event in super().astream_events(*args, **kwargs):
                if event["event"] == "on_chat_model_stream":
                    # 与真实 provider hook 相同，先提交参数，再交付外层模型事件。
                    await case.runtime.accept_message_chunk(
                        event["data"]["chunk"], model_call_id="model_streaming_args",
                    )
                yield event

    agent = StreamingArgumentsAgent([
        (0, {"event": "on_chat_model_start", "data": {}, **model_base}),
        (0, {"event": "on_chat_model_stream", "data": {"chunk": fragments[0]}, **model_base}),
        (0.08, {"event": "on_chat_model_stream", "data": {"chunk": fragments[1]}, **model_base}),
        (0, {"event": "on_chat_model_end", "data": {}, **model_base}),
        (0, {"event": "on_tool_start", "name": "read_file", "run_id": "streamed_tool",
             "data": {"input": {"path": "fixture.txt"}}, "metadata": {}}),
        (0, {"event": "on_tool_end", "name": "read_file", "run_id": "streamed_tool",
             "data": {"output": ToolMessage(content="原结果", tool_call_id="call_streaming_args", name="read_file")},
             "metadata": {}}),
    ])
    agent.tool_invocations["streamed_tool"] = ("model_streaming_args", "call_streaming_args")
    await case.process(agent, timeout=0.03)
    assert case.runtime.pending_tool_calls() == ()
    assert case.runtime._completed_tool_result_call_ids == {"model_streaming_args:tool-call:call_streaming_args"}


@pytest.mark.asyncio
async def test_dispatch_stream_exhaustion_fails_only_unclaimed_calls(dispatch_watchdog_case):
    case = dispatch_watchdog_case
    with pytest.raises(AgentEventStreamTimeoutError, match="事件流结束时仍存在未分派") as raised:
        await case.process(FakeAgent([case.starts[0]]))
    assert raised.value.code == "tool_dispatch_timeout"
    incomplete = [call.args[1] for call in case.writer.commit.await_args_list
                  if call.args[0] == "tool_call.completed" and call.args[1]["status"] == "incomplete"]
    assert [event["tool_call_id"] for event in incomplete] == case.call_ids[1:]
    assert set(case.runtime._active_tool_executions) == {"run_0"}


@pytest.mark.asyncio
async def test_user_cancellation_after_dispatch_progress_keeps_user_reason(
    dispatch_watchdog_case, cancellation_signal: CancellationSignal,
):
    case = dispatch_watchdog_case

    class CancellingAgent(FakeAgent):
        async def astream_events(self, *args, **kwargs):
            async for event in super().astream_events(*args, **kwargs):
                yield event
            await cancellation_signal.cancel("user_requested")
            raise ScopeCancelledError("user_requested")

    with pytest.raises(asyncio.CancelledError, match="user_requested"):
        await case.process(CancellingAgent([case.starts[0]]), cancellation_signal=cancellation_signal)
    assert not [call for call in case.writer.commit.await_args_list
                if call.args[0] == "tool_call.completed" and call.args[1]["status"] == "incomplete"]
    assert [item[0] for item in case.runtime.pending_tool_calls()] == case.call_ids[1:]


@pytest.mark.asyncio
async def test_tool_dispatch_timeout_does_not_escape_as_agent_cancellation(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_dispatch_timeout")
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_dispatch_timeout", "backup_3")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_dispatch_timeout",
                    "name": "exec_command",
                    "args": '{"cmd":"pwd"}',
                }
            ],
        )
    )

    with pytest.raises(AgentEventStreamTimeoutError) as raised:
        await process_agent_event_stream(
            agent=CancellationSensitiveAgent(),
            input_payload={"messages": []},
            config={},
            session_id="ses_dispatch_timeout",
            job_id="job_dispatch_timeout",
            turn_id="job_dispatch_timeout",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=FakeSessionChangesService(),
            workspace_root=tmp_path,
            message_stream_runtime=runtime,
            tool_dispatch_timeout_seconds=0.01,
            model_timeout_seconds=1,
        )

    assert raised.value.code == "tool_dispatch_timeout"
    completed = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    ]
    expected_tool_call_id = (
        "model_dispatch_timeout:tool-call:call_dispatch_timeout"
    )
    assert completed[-1] == {
        "tool_call_id": expected_tool_call_id,
        "tool_invocation_id": (
            "tool-invocation:model_dispatch_timeout:call_dispatch_timeout"
        ),
        "tool_name": "exec_command",
        "status": "incomplete",
        "completion_reason": "tool_dispatch_timeout",
        "arguments_complete": True,
        "error": (
            "模型工具调用参数已完整，但在有限时间内没有收到工具执行分派事件: "
            f"tool_calls=['{expected_tool_call_id}']"
        ),
    }


@pytest.mark.asyncio
async def test_provider_scope_cancellation_from_event_iterator_reaches_agent_loop(
    tmp_path: Path,
    cancellation_signal: CancellationSignal,
) -> None:
    class CancelledAgent:
        async def astream_events(
            self,
            _input_payload: dict[str, Any],
            *,
            config: dict[str, Any],
            version: str,
        ) -> AsyncIterator[dict[str, Any]]:
            del config, version
            if False:
                yield {}
            raise ScopeCancelledError("user_requested")

    await cancellation_signal.cancel("user_requested")

    with pytest.raises(asyncio.CancelledError, match="user_requested"):
        await process_agent_event_stream(
            agent=CancelledAgent(),
            input_payload={"messages": []},
            config={},
            session_id="ses_provider_cancelled",
            job_id="job_provider_cancelled",
            turn_id="job_provider_cancelled",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=FakeSessionChangesService(),
            workspace_root=tmp_path,
            cancellation_signal=cancellation_signal,
        )


@pytest.mark.asyncio
async def test_tool_start_event_contains_only_model_visible_arguments(
    tmp_path: Path,
) -> None:
    @tool
    def runtime_aware(value: str, runtime: ToolRuntime) -> str:
        """测试含运行时注入参数的工具。"""
        del runtime
        return value

    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_runtime_aware",
            "name": "runtime_aware",
            "data": {
                "input": {
                    "value": "ready",
                    "runtime": object(),
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_runtime_aware",
            "name": "runtime_aware",
            "data": {
                "output": ToolMessage(
                    content="ready",
                    tool_call_id="call_runtime_aware",
                    name="runtime_aware",
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    await process_agent_event_stream(
        agent=GraphFakeAgent(events, [runtime_aware]),
        input_payload={"messages": []},
        config={},
        session_id="ses_runtime_args",
        job_id="job_runtime_args",
        turn_id="job_runtime_args",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
    )

    start_payload = published[0][1]
    assert start_payload["args"] == {"value": "ready"}
    json.dumps(start_payload)


@pytest.fixture
def schema_send_tool() -> BaseTool:
    return create_send_message_to_session_tool(
        "ses_schema_observer",
        communication_service=AsyncMock(),
        invocation_context=ToolInvocationContext(),
    )


@pytest.mark.asyncio
async def test_parallel_schema_errors_keep_original_provider_arguments_and_claims(
    tmp_path: Path,
    schema_send_tool: BaseTool,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock())
    canonical_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=canonical_sink,
        tool_result_sink=tool_result_sink,
    )
    await runtime.start_model("model_schema_calls", "primary")
    base = {"target_session_id": "ses_target", "content": "保留原调用参数。"}
    arguments = [
        dict(base),
        {**base, "target_workspace_id": None, "kind": "result",
         "reply_to_communication_id": None, "delivery_policy": "after_turn"},
        *({**base, field: {"status": "completed"}}
          for field in ("team_state", "team_task", "goal_state")),
    ]
    call_ids = [f"call_schema_{index}" for index in range(len(arguments))]
    await runtime.accept_message_chunk(AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"index": index, "id": call_ids[index], "name": schema_send_tool.name,
             "args": json.dumps(args, ensure_ascii=False)}
            for index, args in enumerate(arguments)
        ],
    ))
    # 同名调用并行开始，并反向结束；精确身份与非法字段的展示彼此独立。
    order = [4, 1, 2, 0, 3]
    events = [
        {"event": "on_tool_start", "run_id": f"run_schema_{index}",
         "name": schema_send_tool.name, "data": {"input": arguments[index]},
         "metadata": {}}
        for index in order
    ]
    events.extend(
        {"event": "on_tool_end", "run_id": f"run_schema_{index}",
         "name": schema_send_tool.name,
         "data": {"output": ToolMessage(
             content=f"schema-error-{index}" if index >= 2 else "已接收。",
             tool_call_id=call_ids[index], name=schema_send_tool.name,
             status="error" if index >= 2 else "success",
         )}, "metadata": {}}
        for index in reversed(order)
    )
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=GraphFakeAgent(events, [schema_send_tool], tool_invocations={
            f"run_schema_{index}": ("model_schema_calls", provider_id)
            for index, provider_id in enumerate(call_ids)
        }),
        input_payload={"messages": []}, config={},
        session_id="ses_schema_observer", job_id="job_schema_observer",
        turn_id="turn_schema_observer", agent_id="default",
        custom_tool_skill_sources={}, publish=publish,
        session_changes_service=FakeSessionChangesService(), workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )
    scoped_ids = [f"model_schema_calls:tool-call:{call_id}" for call_id in call_ids]
    started = [call.args[1] for call in writer.commit.await_args_list
               if call.args[0] == "tool.started"]
    assert [payload["tool_call_id"] for payload in started] == [scoped_ids[index] for index in order]
    assert runtime._tool_call_arguments_by_id == dict(zip(scoped_ids, arguments, strict=True))
    shown_args = [payload["args"] for event, payload in published
                  if event == EventType.TOOL_CALL_START]
    assert shown_args == [arguments[index] for index in order]
    json.dumps(shown_args)
    completions: list[ToolResultCompletion] = [
        call.args[0] for call in tool_result_sink.await_args_list
    ]
    assert len(completions) == len(arguments)
    assert [completion.tool_call_id for completion in completions] == [
        scoped_ids[index] for index in reversed(order)
    ]
    assert [completion.tool_attempt_id for completion in completions] == [
        f"run_schema_{index}" for index in reversed(order)
    ]
    assert all(completion.model_call_id == "model_schema_calls" for completion in completions)
    assert all(completion.tool_name == schema_send_tool.name for completion in completions)
    assert [completion.tool_invocation_id for completion in completions] == [
        f"tool-invocation:model_schema_calls:call_schema_{index}"
        for index in reversed(order)
    ]
    assert [completion.content for completion in completions] == [
        f"schema-error-{index}" if index >= 2 else "已接收。"
        for index in reversed(order)
    ]
    assert [completion.tool_outcome for completion in completions] == [
        "failure" if index >= 2 else "success" for index in reversed(order)
    ]
    assert len(result.successful_tool_calls) == 2


@pytest.mark.asyncio
async def test_custom_tool_execution_keeps_provider_tool_call_identity(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock())
    canonical_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=canonical_sink,
        tool_result_sink=tool_result_sink,
    )
    await runtime.start_model("model_unknown_tool", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                        "index": 0,
                        "id": "call_unknown_tool",
                        "name": "invoke_extension_tool",
                        "args": (
                            '{"tool_name":"totally_unknown_tool",'
                            '"arguments":{}}'
                        ),
                }
            ],
        )
    )
    # 上面的 provider start/delta 直接驱动 runtime；补上对应的真实 model-end
    # 事件，让 processor 封存该次调用的 canonical tool-call item。
    events = [
        {
            "event": "on_chat_model_end",
            "run_id": "model_unknown_tool",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {"output": AIMessage(content="")},
            "metadata": {},
        },
        {
            "event": "on_tool_start",
            "run_id": "run_unknown_tool",
            "name": "invoke_extension_tool",
            "data": {
                "input": {
                    "tool_name": "totally_unknown_tool",
                    "arguments": {},
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_unknown_tool",
            "name": "invoke_extension_tool",
            "data": {
                "output": ToolMessage(
                    content="unknown tool",
                    tool_call_id="call_unknown_tool",
                    name="invoke_extension_tool",
                )
            },
            "metadata": {},
        },
    ]

    await process_agent_event_stream(
        agent=FakeAgent(events, tool_invocations={
            "run_unknown_tool": ("model_unknown_tool", "call_unknown_tool"),
        }),
        input_payload={"messages": []},
        config={},
        session_id="ses_unknown_tool",
        job_id="job_unknown_tool",
        turn_id="job_unknown_tool",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )

    stream_events = [call.args for call in writer.commit.await_args_list]
    tool_started = next(payload for event_type, payload, *_ in stream_events if event_type == "tool.started")
    tool_completed = next(payload for event_type, payload, *_ in stream_events if event_type == "tool.completed")
    expected_tool_call_id = "model_unknown_tool:tool-call:call_unknown_tool"
    assert tool_started["tool_call_id"] == expected_tool_call_id
    assert tool_started["tool_name"] == "totally_unknown_tool"
    assert tool_completed["tool_call_id"] == expected_tool_call_id
    assert tool_completed["tool_name"] == "totally_unknown_tool"
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model_unknown_tool"
    assert completion.tool_call_id == expected_tool_call_id
    assert completion.tool_invocation_id == (
        "tool-invocation:model_unknown_tool:call_unknown_tool"
    )
    assert completion.tool_attempt_id == "run_unknown_tool"
    assert completion.tool_outcome == "success"
    assert completion.content == "unknown tool"
    assert completion.tool_name == tool_completed["tool_name"]
    tool_call_items = [
        item
        for call in canonical_sink.await_args_list
        for item in call.args[0]
        if item.semantic_kind == "tool_call"
    ]
    assert len(tool_call_items) == 1
    assert tool_call_items[0].payload["tool_call_id"] == expected_tool_call_id
    assert tool_call_items[0].payload["name"] == "invoke_extension_tool"


@pytest.mark.asyncio
async def test_envelope_tool_claim_uses_exact_provider_id_and_rejects_missing_call() -> None:
    writer = SimpleNamespace(commit=AsyncMock())
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_envelope_claim", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_list_breakpoints",
                    "name": "invoke_extension_tool",
                    "args": (
                        '{"tool_name":"list_breakpoints",'
                        '"arguments":{}}'
                    ),
                },
                {
                    "index": 1,
                    "id": "call_start_debugging",
                    "name": "invoke_extension_tool",
                    "args": (
                        '{"tool_name":"start_debugging",'
                        '"arguments":{"fileFullPath":"app.mjs",'
                        '"workingDirectory":"."}}'
                    ),
                },
            ],
        )
    )

    assert (
        runtime.claim_tool_call_id(
            "model_envelope_claim", "call_list_breakpoints",
        )
        == "model_envelope_claim:tool-call:call_list_breakpoints"
    )
    assert (
        runtime.claim_tool_call_id(
            "model_envelope_claim", "call_start_debugging",
        )
        == "model_envelope_claim:tool-call:call_start_debugging"
    )
    with pytest.raises(RuntimeError, match="缺少原 emitting ModelCall/ToolCall"):
        runtime.claim_tool_call_id(
            "model_envelope_claim", "call_missing_tool",
        )


def test_last_model_token_usage_keeps_last_execution_request() -> None:
    first = ModelTokenUsagePayload(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cache_read_input_tokens=80,
        model_calls=1,
        reported_model_calls=1,
    )
    last = ModelTokenUsagePayload(
        input_tokens=140,
        output_tokens=10,
        total_tokens=150,
        cache_read_input_tokens=0,
        model_calls=1,
        reported_model_calls=1,
    )

    assert last_model_token_usage([first, last]) == last


@pytest.mark.asyncio
async def test_model_failed_custom_event_is_published_to_trace(tmp_path: Path) -> None:
    events = [
        {
            "event": "on_chat_model_start",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {},
            "metadata": {},
        },
        {
            "event": "on_custom_event",
            "name": "boxteam_model_failed",
            "data": {
                "provider_id": "primary",
                "model": "big-pickle",
                "error_type": "RateLimitError",
                "error": "Rate limit exceeded",
            },
            "metadata": {},
        }
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    writer = SimpleNamespace(commit=AsyncMock())
    runtime = MessageStreamRuntime(writer)
    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_failed",
        job_id="job_model_failed",
        turn_id="job_model_failed",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )

    assert published == [
        (
            EventType.LLM_REQUEST,
            {
                "model": "unknown_model",
                "timestamp": published[0][1]["timestamp"],
            },
        ),
        (
            EventType.MODEL_FAILED,
            {
                "provider_id": "primary",
                "model": "big-pickle",
                "error_type": "RateLimitError",
                "error": "Rate limit exceeded",
            },
        )
    ]
    stream_events = [call.args for call in writer.commit.await_args_list]
    assert [event_type for event_type, _payload, *_rest in stream_events] == [
        "model.started",
        "model.failed",
    ]
    assert stream_events[1][1]["outcome"] == "upstream_error"


@pytest.fixture
def parallel_tool_events() -> list[dict[str, Any]]:
    return [
        {
            "event": "on_tool_start",
            "run_id": "run_a",
            "name": "read_file",
            "data": {"input": {"file_path": "a.txt"}},
            "metadata": {},
        },
        {
            "event": "on_tool_start",
            "run_id": "run_b",
            "name": "grep",
            "data": {"input": {"query": "needle"}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_a",
            "name": "read_file",
            "data": {
                "output": ToolMessage(
                    content="a result",
                    tool_call_id="call_a",
                    name="read_file",
                )
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_b",
            "name": "grep",
            "data": {
                "output": ToolMessage(
                    content="b result",
                    tool_call_id="call_b",
                    name="grep",
                )
            },
            "metadata": {},
        },
    ]


@pytest.fixture
def session_changes_service() -> FakeSessionChangesService:
    return FakeSessionChangesService()


@pytest.fixture
def failing_event_source(request: pytest.FixtureRequest):
    failure_stage, primary_kind = request.param
    primary_error = (
        asyncio.CancelledError("原事件取消")
        if primary_kind == "cancelled" else RuntimeError("原事件失败")
    )
    cleanup_error = OSError("原迭代器关闭失败")

    class FailingEventSource:
        def __init__(self):
            self.closed = False

        def astream_events(self, *_args, **_kwargs):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if failure_stage == "iterator":
                raise primary_error
            return {
                "event": "on_chat_model_start", "name": "BoxteamLiteLLMChatModel",
                "run_id": "cleanup-model", "data": {}, "metadata": {
                    "boxteam_session_id": "ses_cleanup", "boxteam_job_id": "job_cleanup",
                },
            }

        async def aclose(self):
            self.closed = True
            raise cleanup_error

    return SimpleNamespace(
        source=FailingEventSource(), primary=primary_error, cleanup=cleanup_error,
        stage=failure_stage,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_event_source",
    [("iterator", "failed"), ("iterator", "cancelled"),
     ("processor", "failed"), ("processor", "cancelled")],
    indirect=True,
)
async def test_stream_cleanup_preserves_primary_error_and_reports_close_failure(
    failing_event_source, tmp_path: Path, session_changes_service: FakeSessionChangesService,
) -> None:
    case = failing_event_source

    def report_progress(_phase):
        raise case.primary

    # processor 分支在消费已 yield 的事件时失败；iterator 分支在 anext 内失败。
    with pytest.raises(type(case.primary)) as raised:
        await process_agent_event_stream(
            agent=case.source, input_payload={"messages": []}, config={},
            session_id="ses_cleanup", job_id="job_cleanup", turn_id="job_cleanup",
            agent_id="default", custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=session_changes_service, workspace_root=tmp_path,
            progress_reporter=report_progress if case.stage == "processor" else None,
        )
    assert raised.value is case.primary
    assert case.source.closed
    assert any(str(case.cleanup) in note for note in case.primary.__notes__)


@pytest.mark.asyncio
async def test_agent_stream_starts_with_isolated_callbacks_and_business_identity(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    agent = FakeAgent([])

    await process_agent_event_stream(
        agent=agent,
        input_payload={"messages": []},
        config={},
        session_id="ses_isolated",
        job_id="job_isolated",
        turn_id="job_isolated",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert len(agent.received_configs) == 1
    stream_config = agent.received_configs[0]
    assert len(stream_config["callbacks"]) == 1
    assert isinstance(
        stream_config["callbacks"][0],
        AgentRunLifecycleCallbackHandler,
    )
    assert stream_config["metadata"]["boxteam_session_id"] == "ses_isolated"
    assert stream_config["metadata"]["boxteam_job_id"] == "job_isolated"


@pytest.mark.asyncio
async def test_agent_stream_does_not_inherit_sender_callback_context(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    inherited_callback = object()
    context_token = var_child_runnable_config.set(
        {"callbacks": [inherited_callback]}
    )
    agent = ResolvingConfigAgent()
    try:
        await process_agent_event_stream(
            agent=agent,
            input_payload={"messages": []},
            config={},
            session_id="ses_target",
            job_id="job_target",
            turn_id="job_target",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=session_changes_service,
            workspace_root=tmp_path,
        )
    finally:
        var_child_runnable_config.reset(context_token)

    assert len(agent.resolved_configs) == 1
    assert len(agent.resolved_configs[0]["callbacks"]) == 1
    assert isinstance(
        agent.resolved_configs[0]["callbacks"][0],
        AgentRunLifecycleCallbackHandler,
    )


@pytest.mark.asyncio
async def test_agent_stream_rejects_model_event_from_another_job(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    agent = FakeAgent(
        [
            {
                "event": "on_chat_model_start",
                "run_id": "model_run_other_job",
                "name": "BoxteamLiteLLMChatModel",
                "data": {},
                "metadata": {
                    "ls_model_name": "primary",
                    "boxteam_session_id": "ses_target",
                    "boxteam_job_id": "job_target",
                },
            }
        ]
    )

    with pytest.raises(RuntimeError, match="跨 Agent job 的 LangChain 事件串入"):
        await process_agent_event_stream(
            agent=agent,
            input_payload={"messages": []},
            config={},
            session_id="ses_sender",
            job_id="job_sender",
            turn_id="job_sender",
            agent_id="default",
            custom_tool_skill_sources={},
            publish=lambda *_args, **_kwargs: _async_noop(),
            session_changes_service=session_changes_service,
            workspace_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_agent_stream_reports_model_and_tool_progress(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    @tool
    def read_file(path: str) -> str:
        """读取测试文件。"""
        return path

    events = [
        {
            "event": "on_chat_model_start",
            "run_id": "model_progress",
            "name": "ChatOpenAI",
            "data": {},
            "metadata": {"ls_model_name": "backup_3"},
        },
        {
            "event": "on_tool_start",
            "run_id": "tool_progress",
            "name": "read_file",
            "data": {"input": {"path": "README.md"}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "tool_progress",
            "name": "read_file",
            "data": {
                "output": ToolMessage(
                    content="README.md",
                    tool_call_id="call_progress",
                    name="read_file",
                )
            },
            "metadata": {},
        },
    ]
    progress: list[str] = []

    await process_agent_event_stream(
        agent=GraphFakeAgent(events, [read_file]),
        input_payload={"messages": []},
        config={},
        session_id="ses_progress_report",
        job_id="job_progress_report",
        turn_id="job_progress_report",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
        progress_reporter=progress.append,
    )

    assert progress == ["model", "tool:read_file", "tool:read_file"]


@pytest.mark.asyncio
async def test_first_parallel_tool_end_does_not_clear_remaining_tool(
    tmp_path: Path,
    parallel_tool_events: list[dict[str, Any]],
    session_changes_service: FakeSessionChangesService,
) -> None:
    session_id = "ses_parallel_event_stream"
    SessionInterruptState.clear(session_id)
    observed_states: list[tuple[str, str | None, str | None, str | None]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        state = SessionInterruptState.get(session_id)
        observed_states.append(
            (
                event_type,
                payload.get("part_id"),
                state.phase,
                state.tool_name,
            )
        )

    await process_agent_event_stream(
        agent=FakeAgent(parallel_tool_events),
        input_payload={"messages": []},
        config={},
        session_id=session_id,
        job_id="job_parallel",
        turn_id="job_parallel",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert observed_states == [
        (EventType.TOOL_CALL_START, "run_a", "tool", "read_file"),
        (EventType.TOOL_CALL_START, "run_b", "tool", "read_file、grep"),
        (EventType.TOOL_CALL_END, "run_a", "tool", "grep"),
        (EventType.TOOL_CALL_END, "run_b", None, None),
    ]
    assert get_interruptible_phase() == "text"
    assert get_active_tool_name() is None
    SessionInterruptState.clear(session_id)


@pytest.mark.asyncio
async def test_failed_tool_message_publishes_failed_tool_call_end(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_failed",
            "name": "invoke_extension_tool",
            "data": {
                "input": {
                    "tool_name": "read_context",
                    "arguments": {
                        "resource_identity": "11111111-1111-5111-8111-111111111111",
                        "uri": "boxteam://workspace/11111111-1111-4111-8111-111111111111/resources/session/ses_target",
                        "scope": "workspace",
                    },
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_failed",
            "name": "invoke_extension_tool",
            "data": {
                "output": ToolMessage(
                    content="资源目标不可解析（target_not_resolvable）；请修正资源 URI 后重试",
                    tool_call_id="call_failed",
                    name="invoke_extension_tool",
                    status="error",
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_failed_tool",
        job_id="job_failed_tool",
        turn_id="job_failed_tool",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert [event_type for event_type, _payload in published] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_END,
    ]
    end_payload = published[1][1]
    assert end_payload["status"] == "error"
    assert end_payload["failed"] is True
    assert end_payload["result"] == result.last_tool_result_text
    assert "target_not_resolvable" in end_payload["result"]
    assert result.successful_tool_calls == ()


@pytest.mark.asyncio
async def test_structured_tool_error_is_not_treated_as_successful_apply_patch(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    """工具函数返回 status=error 时，不应再尝试读取 apply_patch journal。"""
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_apply_patch_error",
            "name": "apply_patch",
            "data": {
                "input": {
                    "input": "*** Begin Patch\n*** Update File: missing.txt\n*** End Patch",
                    "explanation": "无效补丁",
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_apply_patch_error",
            "name": "apply_patch",
            "data": {
                "output": ToolMessage(
                    content=json.dumps(
                        {
                            "status": "error",
                            "error_type": "DiffError",
                            "error": "补丁上下文无效",
                            "retryable": True,
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id="call_apply_patch_error",
                    name="apply_patch",
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_apply_patch_error",
        job_id="job_apply_patch_error",
        turn_id="job_apply_patch_error",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert published[1][1]["status"] == "error"
    assert published[1][1]["failed"] is True
    assert result.successful_tool_calls == ()


@pytest.mark.asyncio
async def test_skill_load_event_does_not_expose_internal_source_metadata(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_skill_load",
            "name": "skill_load",
            "data": {
                "input": {
                    "name": "browser-control",
                    "mode": "snapshot",
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_skill_load",
            "name": "skill_load",
            "data": {
                "output": ToolMessage(
                    content='{"status":"success","name":"browser-control","mode":"snapshot","revision":"sha256:test","queued":true,"tracked":false}',
                    tool_call_id="call_skill_load",
                    name="skill_load",
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_system_skill",
        job_id="job_system_skill",
        turn_id="job_system_skill",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    payload = published[1][1]
    assert payload["tool_name"] == "skill_load"
    assert "path" not in payload
    assert "internal_locator" not in payload


@pytest.mark.asyncio
async def test_command_tool_output_extracts_nested_tool_message(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_todos",
            "name": "write_todos",
            "data": {"input": {"todos": []}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_todos",
            "name": "write_todos",
            "data": {
                "output": Command(
                    update={
                        "todos": [],
                        "messages": [
                            ToolMessage(
                                content="Updated todo list",
                                tool_call_id="call_todos",
                                name="write_todos",
                            )
                        ],
                    }
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_command_tool",
        job_id="job_command_tool",
        turn_id="job_command_tool",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert [event_type for event_type, _payload in published] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_END,
    ]
    assert published[1][1]["tool_call_id"] == "call_todos"
    assert published[1][1]["result"] == "Updated todo list"
    assert result.successful_tool_calls[0].tool_name == "write_todos"


@pytest.mark.asyncio
async def test_successful_tool_call_keeps_arguments_for_delegation_validation(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_send",
            "name": "send_message_to_session",
            "data": {
                "input": {
                    "target_session_id": "ses_parent",
                    "content": "完成",
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_send",
            "name": "send_message_to_session",
            "data": {
                "output": ToolMessage(
                    content="accepted",
                    tool_call_id="call_send",
                    name="send_message_to_session",
                )
            },
            "metadata": {},
        },
    ]

    async def publish(_event_type: str, _payload: dict[str, Any]) -> None:
        return None

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_child",
        job_id="job_child",
        turn_id="job_child",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert len(result.successful_tool_calls) == 1
    call = result.successful_tool_calls[0]
    assert call.tool_name == "send_message_to_session"
    assert call.tool_args["target_session_id"] == "ses_parent"


@pytest.mark.asyncio
async def test_file_edit_keeps_model_tool_call_id_and_execution_id_separate(
    tmp_path: Path,
) -> None:
    changes = RecordingSessionChangesService()
    published: list[tuple[str, dict[str, Any]]] = []
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_write",
            "name": "write_file",
            "data": {"input": {"file_path": "src/example.txt", "content": "ok"}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_write",
            "name": "write_file",
            "data": {
                "output": ToolMessage(
                    content="写入成功",
                    tool_call_id="call_write",
                    name="write_file",
                )
            },
            "metadata": {},
        },
    ]

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_tool_identity",
        job_id="job_tool_identity",
        turn_id="job_tool_identity",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=changes,
        workspace_root=tmp_path,
    )

    assert len(changes.recorded) == 1
    assert changes.recorded[0]["tool_call_id"] == "call_write"
    assert changes.recorded[0]["execution_id"] == "run_write"
    assert published[0][1]["part_id"] == "run_write"
    assert published[0][1]["execution_id"] == "run_write"
    assert published[1][1]["tool_call_id"] == "call_write"
    assert published[1][1]["execution_id"] == "run_write"


@pytest.mark.asyncio
async def test_internal_file_edit_keeps_job_identity_without_turn_attribution(
    tmp_path: Path,
) -> None:
    changes = RecordingSessionChangesService()
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_internal_write",
            "name": "write_file",
            "data": {"input": {"file_path": "src/internal.txt", "content": "ok"}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_internal_write",
            "name": "write_file",
            "data": {
                "output": ToolMessage(
                    content="写入成功",
                    tool_call_id="call_internal_write",
                    name="write_file",
                )
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_internal_tool_identity",
        job_id="job_internal_tool_identity",
        turn_id=None,
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=changes,
        workspace_root=tmp_path,
    )

    assert len(changes.recorded) == 1
    assert changes.recorded[0]["turn_id"] is None
    assert changes.recorded[0]["execution_id"] == "run_internal_write"
    assert changes.recorded[0]["tool_call_id"] == "call_internal_write"
    assert published[0][1]["execution_id"] == "run_internal_write"
    assert published[1][1]["execution_id"] == "run_internal_write"


@pytest.mark.asyncio
async def test_small_model_chunks_only_feed_final_text_aggregation(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "text",
                            "text": text,
                            "id": "part_coalesced",
                            "index": 0,
                        }
                    ]
                )
            },
            "metadata": {},
        }
        for text in ("a", "b", "c")
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_coalesced_delta",
        job_id="job_coalesced_delta",
        turn_id="job_coalesced_delta",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert result.final_text == "abc"
    assert published == []


@pytest.mark.asyncio
async def test_model_end_output_recovers_text_missing_from_stream_callback(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_chat_model_start",
            "run_id": "model_end_only",
            "name": "BoxteamLiteLLMChatModel",
            "data": {},
            "metadata": {},
        },
        {
            "event": "on_chat_model_end",
            "run_id": "model_end_only",
            "name": "BoxteamLiteLLMChatModel",
            "data": {
                "output": AIMessage(content="工具前的说明文字"),
            },
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    writer = SimpleNamespace(commit=AsyncMock())
    runtime = MessageStreamRuntime(
        writer,
        normalized_block_observer=MessageStreamTraceObserver(publish).observe,
    )
    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_end_only",
        job_id="job_model_end_only",
        turn_id="job_model_end_only",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )

    assert result.final_text == "工具前的说明文字"
    assert [
        event_type
        for event_type, _payload in published
        if event_type != EventType.LLM_REQUEST
    ] == [
        EventType.TEXT_START,
        EventType.TEXT_DELTA,
        EventType.TEXT_END,
    ]
    text_delta_payloads = [
        payload
        for event_type, payload in published
        if event_type == EventType.TEXT_DELTA
    ]
    assert len(text_delta_payloads) == 1
    assert text_delta_payloads[0]["text"] == "工具前的说明文字"


@pytest.mark.asyncio
async def test_summary_candidate_completion_uses_public_outcome_and_hides_candidate_text(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    model_call_id = "model_summary_candidate"
    candidate_text = "仅供压缩中间件校验的摘要候选"
    events = [
        {
            "event": "on_chat_model_start",
            "run_id": model_call_id,
            "name": "BoxteamLiteLLMChatModel",
            "data": {},
            "metadata": {},
        },
        {
            "event": "on_chat_model_end",
            "run_id": model_call_id,
            "name": "BoxteamLiteLLMChatModel",
            "data": {"output": AIMessage(content=candidate_text)},
            "metadata": {},
        },
    ]
    writer = SimpleNamespace(
        commit=AsyncMock(),
        turn_stream_id="stream_summary_candidate",
    )
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    runtime = MessageStreamRuntime(
        writer,
        normalized_block_observer=MessageStreamTraceObserver(publish).observe,
    )
    runtime.register_summary_candidate(model_call_id)

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_019bef80066f7038ae8e88ccb2e5e2ea",
        job_id="job_summary_candidate",
        turn_id="turn_summary_candidate",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )

    committed = [call.args for call in writer.commit.await_args_list]
    assert [event_type for event_type, _payload in committed] == [
        "model.started",
        "model.completed",
    ]
    completed_payload = committed[-1][1]
    assert completed_payload == {
        "model_call_id": model_call_id,
        "attempt": 1,
        "outcome": "accepted",
        "reason": "摘要候选传输完成；正文是否可用由压缩中间件校验",
    }

    encoded = message_stream_to_proto(
        {
            "event_id": "evt_summary_candidate_completed",
            "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "thread_id": "thr_00000000400070008000000000000002",
            "turn_id": "turn_summary_candidate",
            "turn_stream_id": "stream_summary_candidate",
            "event_seq": 2,
            "type": "model.completed",
            "model_call_id": model_call_id,
            "payload": completed_payload,
        }
    )
    public_event = message_stream_to_json(encoded)
    assert public_event["payload"]["outcome"] == "accepted"
    assert public_event["payload"]["reason"] == completed_payload["reason"]

    assert candidate_text not in result.final_text
    assert not any(event_type.startswith("block.") for event_type, _payload in committed)
    assert not any(event_type.startswith("block.") for event_type, _payload in published)
    assert all(candidate_text not in repr(payload) for _event_type, payload in committed)


@pytest.mark.asyncio
async def test_model_end_does_not_replay_text_already_committed_by_provider_hook(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_end_dedup")
    published: list[tuple[str, dict[str, Any]]] = []

    async def trace_publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    runtime = MessageStreamRuntime(
        writer,
        normalized_block_observer=MessageStreamTraceObserver(trace_publish).observe,
    )

    class ProviderCommittedAgent:
        async def astream_events(
            self,
            _input_payload: dict[str, Any],
            *,
            config: dict[str, Any],
            version: str,
        ) -> AsyncIterator[dict[str, Any]]:
            del version
            event_metadata = config["metadata"]
            yield {
                "event": "on_chat_model_start",
                "run_id": "model_end_dedup",
                "name": "BoxteamLiteLLMChatModel",
                "data": {},
                "metadata": event_metadata,
            }
            await runtime.accept_message_chunk(
                AIMessageChunk(
                    content=[
                        {
                            "type": "text",
                            "id": "provider-text-1",
                            "index": 0,
                            "text": "已经提交的正文",
                        }
                    ]
                ),
                model_call_id="model_end_dedup",
            )
            yield {
                "event": "on_chat_model_end",
                "run_id": "model_end_dedup",
                "name": "BoxteamLiteLLMChatModel",
                "data": {
                    "output": AIMessage(content="已经提交的正文"),
                },
                "metadata": event_metadata,
            }

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=ProviderCommittedAgent(),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_end_dedup",
        job_id="job_model_end_dedup",
        turn_id="job_model_end_dedup",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
        message_stream_runtime=runtime,
    )

    assert result.final_text == "已经提交的正文"
    assert [
        payload["text"]
        for event_type, payload in published
        if event_type == EventType.TEXT_DELTA
    ] == ["已经提交的正文"]
    assert [
        payload.get("text")
        for call in writer.commit.await_args_list
        if call.args and call.args[0] == "block.delta"
        for payload in [call.args[1]]
    ] == ["已经提交的正文"]


@pytest.mark.asyncio
async def test_resumed_authoritative_text_part_keeps_all_text_without_legacy_realtime(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    def text_chunk(text: str, *, part_id: str, index: int) -> AIMessageChunk:
        return AIMessageChunk(
            content=[
                {
                    "type": "text",
                    "text": text,
                    "id": part_id,
                    "index": index,
                }
            ]
        )

    events = [
        {
            "event": "on_chat_model_stream",
            "name": "BoxteamLiteLLMChatModel",
            "data": {"chunk": text_chunk("前半", part_id="part_shared", index=0)},
            "metadata": {},
        },
        {
            "event": "on_chat_model_stream",
            "name": "BoxteamLiteLLMChatModel",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "reasoning",
                            "reasoning": "思考",
                            "id": "part_reasoning",
                            "index": 1,
                        }
                    ]
                )
            },
            "metadata": {},
        },
        {
            "event": "on_chat_model_stream",
            "name": "BoxteamLiteLLMChatModel",
            "data": {"chunk": text_chunk("后半", part_id="part_shared", index=0)},
            "metadata": {},
        },
    ]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        published.append((event_type, payload))

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_resumed_text_part",
        job_id="job_resumed_text_part",
        turn_id="job_resumed_text_part",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    shared_events = [
        (event_type, payload)
        for event_type, payload in published
        if payload.get("part_id") == "part_shared"
    ]
    assert shared_events == []
    assert result.final_text == "前半后半"


@pytest.mark.asyncio
async def test_model_stream_usage_keeps_only_last_call(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_chat_model_start",
            "run_id": "model_run_1",
            "name": "BoxteamLiteLLMChatModel",
            "data": {},
            "metadata": {"ls_model_name": "primary"},
        },
        {
            "event": "on_chat_model_stream",
            "run_id": "model_run_1",
            "name": "BoxteamLiteLLMChatModel",
            "data": {
                "chunk": AIMessageChunk(
                    content="",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "input_token_details": {"cache_read": 80},
                    },
                )
            },
            "metadata": {},
        },
        {
            "event": "on_chat_model_start",
            "run_id": "model_run_2",
            "name": "BoxteamLiteLLMChatModel",
            "data": {},
            "metadata": {"ls_model_name": "primary"},
        },
        {
            "event": "on_chat_model_stream",
            "run_id": "model_run_2",
            "name": "BoxteamLiteLLMChatModel",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "text",
                            "text": "OK",
                            "id": "part_usage_answer",
                            "index": 0,
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 140,
                        "output_tokens": 10,
                        "total_tokens": 150,
                        "input_token_details": {"cache_read": 100},
                    },
                )
            },
            "metadata": {},
        },
    ]

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_token_usage",
        job_id="job_token_usage",
        turn_id="job_token_usage",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert result.final_text == "OK"
    assert result.token_usage.model_dump() == {
        "input_tokens": 140,
        "output_tokens": 10,
        "total_tokens": 150,
        "cache_read_input_tokens": 100,
        "model_calls": 1,
        "reported_model_calls": 1,
    }


@pytest.mark.asyncio
async def test_empty_reasoning_done_preserves_encrypted_response_item(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    response_item = {
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    events = [
        {
            "event": "on_chat_model_stream",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "reasoning",
                            "reasoning": "",
                            "id": "part_reasoning",
                            "index": 0,
                            "extras": {"response_item": response_item},
                        }
                    ]
                )
            },
            "metadata": {},
        },
        {
            "event": "on_chat_model_stream",
            "name": "BoxteamOpenAIResponsesModel",
            "data": {
                "chunk": AIMessageChunk(
                    content=[
                        {
                            "type": "text",
                            "text": "完成",
                            "id": "part_text",
                            "index": 1,
                        }
                    ]
                )
            },
            "metadata": {},
        },
    ]

    result = await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_encrypted_reasoning",
        job_id="job_encrypted_reasoning",
        turn_id="job_encrypted_reasoning",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert result.final_text == "完成"
    assert result.latest_model_content_blocks[0]["extras"] == {
        "response_item": response_item
    }


@pytest.mark.asyncio
async def test_task_tool_is_projected_as_subagent_activity(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_subagent")
    message_stream_runtime = MessageStreamRuntime(writer)
    await message_stream_runtime.start_model("model_subagent", "primary")
    await message_stream_runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_task",
                    "name": "task",
                    "args": '{"description":"检查认证"}',
                }
            ],
        )
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_task",
            "name": "task",
            "data": {"input": {"description": "检查认证"}},
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_task",
            "name": "task",
            "data": {
                "output": ToolMessage(
                    content=(
                        '{"child_thread_id":"thr_'
                        + "a" * 32
                        + '","status":"accepted"}'
                    ),
                    tool_call_id="call_task",
                    name="task",
                )
            },
            "metadata": {},
        },
    ]

    await process_agent_event_stream(
        agent=FakeAgent(events, tool_invocations={
            "run_task": ("model_subagent", "call_task"),
        }),
        input_payload={"messages": []},
        config={},
        session_id="ses_parent",
        job_id="job_parent",
        turn_id="job_parent",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        message_stream_runtime=message_stream_runtime,
    )

    activity_events = [
        call.args
        for call in writer.commit.await_args_list
        if call.args[0].startswith("activity.")
    ]
    assert [event[0] for event in activity_events] == [
        "activity.started",
        "activity.updated",
        "activity.completed",
    ]
    assert activity_events[0][1]["kind"] == "subagent.run"
    assert activity_events[1][1]["detail"]["child_turn_id"] == "thr_" + "a" * 32


@pytest.mark.asyncio
async def test_resource_tool_is_projected_as_resource_activity(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_resource")
    message_stream_runtime = MessageStreamRuntime(writer)
    await message_stream_runtime.start_model("model_resource", "primary")
    await message_stream_runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_page",
                    "name": "readPage",
                    "args": '{"pageId":"browser_1"}',
                }
            ],
        )
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_page",
            "name": "readPage",
            "data": {"input": {"pageId": "browser_1"}},
            "metadata": {
                "resource_activity": {"resource_id": "browser_1"},
            },
        },
        {
            "event": "on_tool_end",
            "run_id": "run_page",
            "name": "readPage",
            "data": {
                "output": ToolMessage(
                    content="页面已读取",
                    tool_call_id="call_page",
                    name="readPage",
                )
            },
            "metadata": {},
        },
    ]

    await process_agent_event_stream(
        agent=FakeAgent(events, tool_invocations={
            "run_page": ("model_resource", "call_page"),
        }),
        input_payload={"messages": []},
        config={},
        session_id="ses_resource",
        job_id="job_resource",
        turn_id="job_resource",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        message_stream_runtime=message_stream_runtime,
    )

    activity_events = [
        call.args
        for call in writer.commit.await_args_list
        if call.args[0].startswith("activity.")
    ]
    assert [event[0] for event in activity_events] == [
        "activity.started",
        "activity.updated",
        "activity.completed",
    ]
    assert activity_events[0][1]["kind"] == "resource.operation"
    assert activity_events[0][1]["resource_refs"] == ["browser_1"]
    assert activity_events[1][1]["detail"]["resource_id"] == "browser_1"


def test_retryable_resource_failure_projects_recovery_metadata() -> None:
    detail = activity_result_detail(
        json.dumps(
            {
                "status": "error",
                "code": "browser_tool_timeout",
                "retryable": True,
                "recovery": "page_reset",
                "timeoutMs": 10000,
                "error": "页面已重置，可重试",
            }
        ),
        tool_name="runPlaywrightCode",
        agent_id="default",
    )

    assert detail == {
        "phase": "failed",
        "agent_id": "default",
        "code": "browser_tool_timeout",
        "retryable": True,
        "recovery": "page_reset",
        "timeout_ms": 10000,
    }


@pytest.mark.asyncio
async def test_retryable_resource_failure_does_not_abort_event_stream(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock(), turn_stream_id="stream_resource_error")
    message_stream_runtime = MessageStreamRuntime(writer)
    await message_stream_runtime.start_model("model_resource_error", "primary")
    await message_stream_runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_page_error",
                    "name": "readPage",
                    "args": '{"pageId":"browser_1"}',
                }
            ],
        )
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_page_error",
            "name": "readPage",
            "data": {"input": {"pageId": "browser_1"}},
            "metadata": {
                "resource_activity": {"resource_id": "browser_1"},
            },
        },
        {
            "event": "on_tool_end",
            "run_id": "run_page_error",
            "name": "readPage",
            "data": {
                "output": ToolMessage(
                    content=json.dumps(
                        {
                            "status": "error",
                            "code": "browser_tool_timeout",
                            "retryable": True,
                            "recovery": "page_reset",
                            "timeoutMs": 10000,
                            "error": "页面已重置，可重试",
                        }
                    ),
                    tool_call_id="call_page_error",
                    name="readPage",
                )
            },
            "metadata": {},
        },
    ]

    result = await process_agent_event_stream(
        agent=FakeAgent(events, tool_invocations={
            "run_page_error": ("model_resource_error", "call_page_error"),
        }),
        input_payload={"messages": []},
        config={},
        session_id="ses_resource_error",
        job_id="job_resource_error",
        turn_id="job_resource_error",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=FakeSessionChangesService(),
        workspace_root=tmp_path,
        message_stream_runtime=message_stream_runtime,
    )

    activity_events = [
        call.args
        for call in writer.commit.await_args_list
        if call.args[0].startswith("activity.")
    ]
    assert result.final_text == ""
    assert activity_events[-1][0] == "activity.failed"
    assert activity_events[-1][1]["outcome"] == "outcome_unknown"
    assert activity_events[-1][1]["detail"] == {
        "resource_id": "browser_1",
        "operation": "readPage",
        "phase": "failed",
        "code": "browser_tool_timeout",
        "retryable": True,
        "recovery": "page_reset",
        "timeout_ms": 10000,
    }


async def _async_noop() -> None:
    return None
