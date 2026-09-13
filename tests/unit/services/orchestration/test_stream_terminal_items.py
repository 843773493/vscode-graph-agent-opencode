"""实时终态到 canonical item 的状态与工具结果合同。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@pytest.fixture
def item_sink() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def runtime(item_sink: AsyncMock) -> MessageStreamRuntime:
    writer = MagicMock()
    writer.commit = AsyncMock()
    return MessageStreamRuntime(writer, canonical_item_sink=item_sink, canonical_turn_id="turn_1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "error", "expected"),
    [
        ("succeeded", "success", None, "success"),
        ("failed", "failure", "工具报错", "failure"),
        ("completed", "cancelled", None, "cancelled"),
        ("completed", "unknown", "用户中断时结果无法确认", "unknown"),
    ],
)
async def test_tool_result_separates_payload_status_from_execution_outcome(
    runtime: MessageStreamRuntime,
    item_sink: AsyncMock,
    status: str,
    outcome: str,
    error: str | None,
    expected: str,
) -> None:
    await runtime.start_model("model_1", "test")
    await runtime.start_tool(tool_execution_id="exec_1", tool_call_id="call_1", tool_name="shell")
    await runtime.complete_tool(
        tool_execution_id="exec_1",
        tool_call_id="call_1",
        tool_name="shell",
        status=status,
        outcome=outcome,
        error=error,
        result="工具结果",
    )

    item_sink.assert_awaited_once()
    item = item_sink.await_args.args[0][0]
    assert item.status == "completed"
    assert item.payload["tool_outcome"] == expected
    assert item.payload["result_id"] == "exec_1"
    assert item.metadata["execution_confirmed"] is (expected != "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "expected"), [("upstream_error", "failed"), ("user_interrupt", "partial")])
async def test_provider_failure_cannot_publish_completed_item(
    runtime: MessageStreamRuntime, item_sink: AsyncMock, outcome: str, expected: str
) -> None:
    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(content=[{"id": "text_1", "index": 0, "type": "text", "text": "尚未完成"}])
    )
    await runtime.fail_model(code="stopped", message="执行终止", outcome=outcome)

    item_sink.assert_awaited_once()
    item = item_sink.await_args.args[0][0]
    assert item.status == expected
    assert item.payload == "尚未完成"
    await runtime.finish_model()
    item_sink.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_result_completion_is_serialized_across_tool_end_and_provider_message(
) -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()
    block_sink = False
    sink_calls: list[tuple[object, ...]] = []

    async def blocked_sink(items: tuple[object, ...]) -> None:
        sink_calls.append(items)
        if not block_sink:
            return
        sink_started.set()
        await release_sink.wait()

    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=blocked_sink,
        canonical_turn_id="turn_1",
    )
    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_1",
                    "name": "shell",
                    "args": '{"command":"pwd"}',
                }
            ],
        )
    )
    await runtime.finish_model()
    await runtime.start_model("model_2", "test")
    await runtime.start_tool(
        tool_execution_id="exec_1",
        tool_call_id="call_1",
        tool_name="shell",
    )
    sink_calls.clear()
    block_sink = True

    tool_end = asyncio.create_task(
        runtime.complete_tool(
            tool_execution_id="exec_1",
            tool_call_id="call_1",
            tool_name="shell",
            status="succeeded",
            result="工具结果",
        )
    )
    await sink_started.wait()
    provider_message = asyncio.create_task(
        runtime.complete_tool_from_message(
            ToolMessage(
                content="工具结果",
                tool_call_id="call_1",
                name="shell",
                status="success",
            )
        )
    )
    await asyncio.sleep(0)
    assert not provider_message.done()
    release_sink.set()
    await asyncio.gather(tool_end, provider_message)

    assert len(sink_calls) == 1
    item = sink_calls[0][0]
    assert item.item_id == "item-exec_1-result-model_1:tool-call:call_1"
    assert item.producer_ref["invocation_id"] == "model_1"
    assert item.payload["tool_invocation_id"] == "tool-invocation:model_1:call_1"
    assert item.payload["tool_attempt_id"] == "exec_1"
    assert item.metadata["model_call_id"] == "model_1"
    assert [
        call.args[0]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool.completed"
    ] == ["tool.completed"]

    with pytest.raises(RuntimeError, match="多个已完成 tool attempt"):
        await runtime.complete_tool(
            tool_execution_id="exec_2",
            tool_call_id="call_1",
            tool_name="shell",
            status="succeeded",
            result="不应覆盖第一条结果",
        )


@pytest.mark.asyncio
async def test_provider_delta_before_model_start_keeps_full_model_call_history(
) -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    item_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=item_sink,
        canonical_turn_id="turn_1",
    )

    # LangChain 的 provider hook 可能先于 on_chat_model_start；真实 run_id
    # 让 runtime 可以把这批 delta 放入尚未登记的对应 model call。
    for text in ("第一段", "第二段", "第三段"):
        await runtime.accept_message_chunk(
            AIMessageChunk(
                content=[
                    {
                        "id": "provider-part-1",
                        "index": 0,
                        "type": "reasoning_content",
                        "reasoning_content": text,
                    }
                ]
            ),
            model_call_id="model_1",
        )

    await runtime.start_model("model_1", "test")
    await runtime.finish_model()

    item_sink.assert_awaited_once()
    item = item_sink.await_args.args[0][0]
    assert item.item_id == (
        "item-model_1-model-model_1-block-model_1:block:provider-part-1"
    )
    assert item.payload == "第一段第二段第三段"
    assert item.producer_ref["invocation_id"] == "model_1"


@pytest.mark.asyncio
async def test_deltas_for_next_model_call_are_not_appended_to_previous_call(
) -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    item_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=item_sink,
        canonical_turn_id="turn_1",
    )

    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "provider-part-1",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第一轮",
                }
            ]
        ),
        model_call_id="model_1",
    )
    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "provider-part-2",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第二轮第一段",
                }
            ]
        ),
        model_call_id="model_2",
    )
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "provider-part-2",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第二轮第二段",
                }
            ]
        ),
        model_call_id="model_2",
    )

    await runtime.start_model("model_2", "test")
    await runtime.finish_model()

    assert len(item_sink.await_args_list) == 2
    first_items = item_sink.await_args_list[0].args[0]
    second_items = item_sink.await_args_list[1].args[0]
    assert first_items[0].payload == "第一轮"
    assert first_items[0].producer_ref["invocation_id"] == "model_1"
    assert second_items[0].payload == "第二轮第一段第二轮第二段"
    assert second_items[0].producer_ref["invocation_id"] == "model_2"


@pytest.mark.asyncio
async def test_stale_provider_run_id_is_bound_to_current_model_call() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    item_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=item_sink,
        canonical_turn_id="turn_1",
    )

    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "first-part",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第一轮",
                }
            ]
        ),
        model_call_id="model_1",
    )
    await runtime.finish_model()

    await runtime.start_model("model_2", "test")
    for text in ("第二轮第一段", "第二轮第二段"):
        await runtime.accept_message_chunk(
            AIMessageChunk(
                content=[
                    {
                        "id": "second-part",
                        "index": 0,
                        "type": "reasoning_content",
                        "reasoning_content": text,
                    }
                ]
            ),
            # 某些 provider hook 在工具循环中错误复用上一层 run id。
            model_call_id="model_1",
        )
    await runtime.finish_model()

    assert len(item_sink.await_args_list) == 2
    second_items = item_sink.await_args_list[1].args[0]
    assert second_items[0].payload == "第二轮第一段第二轮第二段"
    assert second_items[0].producer_ref["invocation_id"] == "model_2"


@pytest.mark.asyncio
async def test_reused_provider_block_id_is_scoped_per_model_call() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    item_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=item_sink,
        canonical_turn_id="turn_1",
    )

    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "provider-reused-part",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第一轮",
                }
            ]
        ),
        model_call_id="model_1",
    )
    await runtime.finish_model()

    await runtime.start_model("model_2", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content=[
                {
                    "id": "provider-reused-part",
                    "index": 0,
                    "type": "reasoning_content",
                    "reasoning_content": "第二轮",
                }
            ]
        ),
        model_call_id="model_2",
    )
    await runtime.finish_model()

    first_item = item_sink.await_args_list[0].args[0][0]
    second_item = item_sink.await_args_list[1].args[0][0]
    assert first_item.item_id != second_item.item_id
    assert first_item.metadata["block_id"] != second_item.metadata["block_id"]
    assert first_item.metadata["model_call_id"] == "model_1"
    assert second_item.metadata["model_call_id"] == "model_2"


@pytest.mark.asyncio
async def test_split_tool_call_keeps_id_when_model_start_event_is_late() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    # provider delta 可能先于 on_chat_model_start 落入同一个 Turn；后续
    # 仅携带 index/arguments 的分段不能因 start 事件而换成空名称调用。
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_split",
                    "name": "read_file",
                    "args": '{"path":',
                }
            ],
        ),
        model_call_id="model_1",
    )
    await runtime.start_model("model_1", "test")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "name": None, "id": None, "args": '"README.md"}'}
            ],
        ),
        model_call_id="model_1",
    )

    tool_deltas = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.delta"
    ]
    assert [payload["tool_call_id"] for payload in tool_deltas] == [
        "model_1:tool-call:call_split",
        "model_1:tool-call:call_split",
    ]
    assert [payload["tool_name"] for payload in tool_deltas] == [
        "read_file",
        "read_file",
    ]
