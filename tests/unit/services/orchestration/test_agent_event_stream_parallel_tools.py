from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.core.job_context import get_active_tool_name, get_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.services.orchestration.agent_event_stream_processor import (
    process_agent_event_stream,
)


class FakeAgent:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def astream_events(
        self,
        input_payload: dict[str, Any],
        *,
        config: dict[str, Any],
        version: str,
    ) -> AsyncIterator[dict[str, Any]]:
        del input_payload, config, version
        for event in self._events:
            yield event


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
                    "tool_name": "read_session_recent_text_messages",
                    "arguments": {
                        "workspace_id": "gw_typo",
                        "session_id": "ses_target",
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
async def test_small_model_chunks_are_coalesced_before_publishing(
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
    assert [event_type for event_type, _ in published] == [
        EventType.TEXT_START,
        EventType.TEXT_DELTA,
    ]
    assert published[1][1]["text"] == "abc"


@pytest.mark.asyncio
async def test_resumed_authoritative_text_part_is_started_once_and_keeps_all_text(
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
    assert [event_type for event_type, _ in shared_events].count(EventType.TEXT_START) == 1
    assert shared_events[-1][0] == EventType.TEXT_DELTA
    assert shared_events[-1][1]["text"] == "后半"
    assert result.final_text == "前半后半"


@pytest.mark.asyncio
async def test_model_stream_usage_is_aggregated_across_calls(
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
        "input_tokens": 240,
        "output_tokens": 30,
        "total_tokens": 270,
        "cache_read_input_tokens": 180,
        "model_calls": 2,
        "reported_model_calls": 2,
    }


async def _async_noop() -> None:
    return None
