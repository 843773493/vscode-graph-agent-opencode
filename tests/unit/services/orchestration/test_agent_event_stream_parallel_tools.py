from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.runnables.config import ensure_config, var_child_runnable_config
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from app.core.job_context import get_active_tool_name, get_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    CancellationSignal,
    ScopeCancelledError,
    TurnExecutionScope,
    get_current_turn_execution_scope,
)
from app.schemas.event import ModelTokenUsagePayload
from app.services.orchestration.agent_event_stream_processor import (
    AgentEventStreamTimeoutError,
    _activity_result_detail,
    last_model_token_usage,
    process_agent_event_stream,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


class FakeAgent:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
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
        for event in self._events:
            event_metadata = event.get("metadata", {})
            if not isinstance(event_metadata, dict):
                raise TypeError("测试事件 metadata 必须为 dict")
            yield {
                **event,
                "metadata": {
                    **config_metadata,
                    **event_metadata,
                },
            }


class GraphFakeAgent(FakeAgent):
    def __init__(self, events: list[dict[str, Any]], tools: list[BaseTool]) -> None:
        super().__init__(events)
        self._tools_by_name = {item.name: item for item in tools}

    def get_graph(self) -> SimpleNamespace:
        return SimpleNamespace(
            nodes={
                "tools": SimpleNamespace(
                    data=SimpleNamespace(tools_by_name=self._tools_by_name),
                )
            }
        )


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
        "app.services.orchestration.agent_event_stream_processor.DEFAULT_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.01,
    )
    result = await process_agent_event_stream(
        agent=ModelStartsThenWaitsAgent(),
        input_payload={"messages": []},
        config={},
        session_id="ses_model_wait_after_start",
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
    assert completed[-1] == {
        "tool_call_id": "call_dispatch_timeout",
        "tool_name": "exec_command",
        "status": "incomplete",
        "completion_reason": "tool_dispatch_timeout",
        "arguments_complete": True,
        "error": (
            "模型工具调用参数已完整，但在有限时间内没有收到工具执行分派事件: "
            "tool_calls=['call_dispatch_timeout']"
        ),
    }


@pytest.mark.asyncio
async def test_provider_scope_cancellation_from_event_iterator_reaches_agent_loop(
    tmp_path: Path,
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

    cancellation_signal = CancellationSignal()
    await cancellation_signal.cancel("user_requested")

    with pytest.raises(asyncio.CancelledError, match="user_requested"):
        await process_agent_event_stream(
            agent=CancelledAgent(),
            input_payload={"messages": []},
            config={},
            session_id="ses_provider_cancelled",
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


@pytest.mark.asyncio
async def test_custom_tool_execution_keeps_provider_tool_call_identity(
    tmp_path: Path,
) -> None:
    writer = SimpleNamespace(commit=AsyncMock())
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_unknown_tool", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_unknown_tool",
                    "name": "invoke_custom_tool",
                    "args": '{"tool_name":"totally_unknown_tool"}',
                }
            ],
        )
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_unknown_tool",
            "name": "invoke_custom_tool",
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
            "name": "invoke_custom_tool",
            "data": {
                "output": ToolMessage(
                    content="unknown tool",
                    tool_call_id="call_unknown_tool",
                    name="invoke_custom_tool",
                )
            },
            "metadata": {},
        },
    ]

    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_unknown_tool",
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
    assert tool_started["tool_call_id"] == "call_unknown_tool"
    assert tool_started["tool_name"] == "totally_unknown_tool"
    assert tool_completed["tool_call_id"] == "call_unknown_tool"


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
        turn_id="job_isolated",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=lambda *_args, **_kwargs: _async_noop(),
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert len(agent.received_configs) == 1
    stream_config = agent.received_configs[0]
    assert stream_config["callbacks"] == []
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
    assert agent.resolved_configs[0]["callbacks"] == []


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
            "name": "invoke_custom_tool",
            "data": {
                "input": {
                    "tool_name": "read_context",
                    "arguments": {
                        "resource": "boxteam://workspace/gw_typo/session/ses_target",
                    },
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_failed",
            "name": "invoke_custom_tool",
            "data": {
                "output": ToolMessage(
                    content="Gateway 工作区不存在: gw_typo；请修正 workspace_id 后重试",
                    tool_call_id="call_failed",
                    name="invoke_custom_tool",
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
    assert "修正 workspace_id" in end_payload["result"]
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
async def test_system_skill_read_is_marked_as_metadata_not_workspace_source(
    tmp_path: Path,
    session_changes_service: FakeSessionChangesService,
) -> None:
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run_skill_read",
            "name": "read_file",
            "data": {
                "input": {
                    "path": ".boxteam/bundled-skills/browser-control/SKILL.md",
                    "line_offset": 1,
                }
            },
            "metadata": {},
        },
        {
            "event": "on_tool_end",
            "run_id": "run_skill_read",
            "name": "read_file",
            "data": {
                "output": ToolMessage(
                    content="# browser-control system skill",
                    tool_call_id="call_skill_read",
                    name="read_file",
                    additional_kwargs={
                        "workspace_path_scope": "system_skill",
                        "workspace_file_kind": "skill_definition",
                        "skill_source": "bundled",
                        "skill_name": "browser-control",
                    },
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
        turn_id="job_system_skill",
        agent_id="default",
        custom_tool_skill_sources={},
        publish=publish,
        session_changes_service=session_changes_service,
        workspace_root=tmp_path,
    )

    assert published[1][1]["workspace_path_scope"] == "system_skill"
    assert published[1][1]["workspace_file_kind"] == "skill_definition"
    assert published[1][1]["skill_source"] == "bundled"
    assert published[1][1]["skill_name"] == "browser-control"


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
                    "simulate_user": False,
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
    assert call.tool_args["simulate_user"] is False


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
                    content='{"child_session_id":"ses_child","status":"accepted"}',
                    tool_call_id="call_task",
                    name="task",
                )
            },
            "metadata": {},
        },
    ]

    await process_agent_event_stream(
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_parent",
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
    assert activity_events[1][1]["detail"]["child_turn_id"] == "ses_child"


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
            "metadata": {},
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
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_resource",
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
    detail = _activity_result_detail(
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
            "metadata": {},
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
        agent=FakeAgent(events),
        input_payload={"messages": []},
        config={},
        session_id="ses_resource_error",
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
