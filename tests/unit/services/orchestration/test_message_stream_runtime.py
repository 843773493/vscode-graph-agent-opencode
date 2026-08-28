import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from app.core.job_event_bus import EventType
from app.services.orchestration.message_stream_runtime import (
    MessageStreamRuntime,
    MessageStreamTraceObserver,
)


@pytest.mark.asyncio
async def test_next_model_attempt_does_not_duplicate_completed_model_call() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    await runtime.complete_model(
        outcome="validation_failed",
        reason="需要重新请求",
    )
    await runtime.retrying("AgentLoop 校验未通过")
    await runtime.start_model("model_2", "primary")

    committed_types = [call.args[0] for call in writer.commit.await_args_list]
    assert committed_types == [
        "model.started",
        "model.completed",
        "model.retrying",
        "model.started",
    ]
    completed_payload = writer.commit.await_args_list[1].args[1]
    assert completed_payload["model_call_id"] == "model_1"
    assert completed_payload["outcome"] == "validation_failed"
    assert runtime.current_model_call_id == "model_2"
    assert runtime.current_attempt == 2


@pytest.mark.asyncio
async def test_tool_call_delta_keeps_identity_and_can_be_claimed_by_tool_execution() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_1",
                    "name": "invoke_custom_tool",
                    "args": '{"tool_name":',
                }
            ],
        )
    )
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "args": '"unknown_tool"}'},
            ],
        )
    )

    first_payload = writer.commit.await_args_list[1].args[1]
    second_payload = writer.commit.await_args_list[2].args[1]
    assert first_payload["tool_name"] == "invoke_custom_tool"
    assert second_payload["tool_call_id"] == "call_1"
    assert second_payload["tool_name"] == "invoke_custom_tool"
    assert second_payload["arguments"] == {"tool_name": "unknown_tool"}
    assert runtime.claim_tool_call_id("invoke_custom_tool") == "call_1"
    assert runtime.claim_tool_call_id("invoke_custom_tool") is None


@pytest.mark.asyncio
async def test_same_name_tool_calls_are_claimed_by_arguments_first() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    for index, path in enumerate(("a.txt", "b.txt")):
        await runtime.accept_message_chunk(
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "index": index,
                        "id": f"call_{index}",
                        "name": "read_file",
                        "args": json.dumps({"path": path}),
                    }
                ],
            )
        )

    assert runtime.claim_tool_call_id("read_file", {"path": "b.txt"}) == "call_1"
    assert runtime.claim_tool_call_id("read_file", {"path": "a.txt"}) == "call_0"


@pytest.mark.asyncio
async def test_normalized_blocks_drive_message_stream_and_trace_projection_once() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    trace_events: list[tuple[str, dict[str, object]]] = []

    async def publish(event_type: str, payload: dict[str, object]) -> None:
        trace_events.append((event_type, payload))

    observer = MessageStreamTraceObserver(publish)
    runtime = MessageStreamRuntime(
        writer,
        normalized_block_observer=observer.observe,
    )

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "type": "text",
                    "id": "answer",
                    "index": 0,
                    "text": "前半",
                }
            ]
        )
    )
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "type": "reasoning",
                    "id": "thinking",
                    "index": 1,
                    "reasoning": "思考",
                },
                {
                    "type": "text",
                    "id": "answer",
                    "index": 0,
                    "text": "后半",
                },
            ]
        )
    )
    await runtime.finish_model()

    assert runtime.normalized_final_text() == "前半后半"
    committed_types = [call.args[0] for call in writer.commit.await_args_list]
    assert committed_types == [
        "model.started",
        "block.started",
        "block.delta",
        "block.completed",
        "block.started",
        "block.delta",
        "block.completed",
        "block.started",
        "block.delta",
        "block.completed",
    ]
    assert [event_type for event_type, _ in trace_events] == [
        EventType.TEXT_START,
        EventType.TEXT_DELTA,
        EventType.TEXT_END,
        EventType.TEXT_START,
        EventType.TEXT_DELTA,
        EventType.TEXT_END,
        EventType.TEXT_START,
        EventType.TEXT_DELTA,
        EventType.TEXT_END,
    ]
    assert [
        (payload["text"], payload["kind"])
        for event_type, payload in trace_events
        if event_type == EventType.TEXT_DELTA
    ] == [
        ("前半", "markdown"),
        ("思考", "reasoning"),
        ("后半", "markdown"),
    ]


@pytest.mark.asyncio
async def test_nested_reasoning_content_is_projected_to_message_stream_and_trace() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    trace_events: list[tuple[str, dict[str, object]]] = []

    async def publish(event_type: str, payload: dict[str, object]) -> None:
        trace_events.append((event_type, payload))

    runtime = MessageStreamRuntime(
        writer,
        normalized_block_observer=MessageStreamTraceObserver(publish).observe,
    )

    await runtime.start_model("model_1", "responses")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "type": "reasoning",
                    "id": "thinking",
                    "index": 0,
                    "content": [
                        {"type": "reasoning_text", "text": "先读取 "},
                        {"type": "reasoning_text", "text": "README"},
                    ],
                }
            ]
        )
    )
    await runtime.finish_model()

    delta_payloads = [
        payload
        for event_type, payload in trace_events
        if event_type == EventType.TEXT_DELTA
    ]
    assert [payload["text"] for payload in delta_payloads] == ["先读取 README"]
    assert runtime.normalized_final_text() == ""


@pytest.mark.asyncio
async def test_interruption_finalizes_partial_blocks_calls_and_unknown_tool_results() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[{
                "type": "reasoning",
                "id": "thinking",
                "index": 0,
                "reasoning": "半截思考",
            }],
            tool_call_chunks=[{
                "index": 0,
                "id": "call_1",
                "name": "shell",
                "args": '{"command":"pwd"',
            }],
        )
    )
    await runtime.start_tool(
        tool_execution_id="exec_1",
        tool_call_id="call_1",
        tool_name="shell",
    )
    await runtime.finalize_interruption_facts()

    calls = [call.args for call in writer.commit.await_args_list]
    assert [call[0] for call in calls] == [
        "model.started",
        "block.started",
        "block.delta",
        "block.completed",
        "tool_call.delta",
        "tool.started",
        "tool_call.completed",
        "tool.completed",
        "model.failed",
    ]
    # tool_call 到来会先闭合 reasoning block，属于 carrier 切换而非中断收尾。
    assert calls[3][1]["partial"] is False
    assert calls[6][1]["status"] == "incomplete"
    assert calls[7][1]["status"] == "completed"
    assert calls[7][1]["outcome"] == "outcome_unknown"
    assert calls[8][1]["outcome"] == "user_interrupt"
    assert calls[8][1]["retryable"] is False

    partial_writer = MagicMock()
    partial_writer.commit = AsyncMock()
    partial_runtime = MessageStreamRuntime(partial_writer)
    await partial_runtime.start_model("model_2", "primary")
    await partial_runtime.accept_message_chunk(
        AIMessageChunk(content=[{
            "type": "text",
            "id": "answer",
            "index": 0,
            "text": "半截文本",
        }])
    )
    await partial_runtime.finalize_interruption_facts()
    partial_completed = [
        call.args[1]
        for call in partial_writer.commit.await_args_list
        if call.args[0] == "block.completed"
    ]
    assert partial_completed[0]["partial"] is True
    assert partial_completed[0]["completion_reason"] == "user_interrupt"
