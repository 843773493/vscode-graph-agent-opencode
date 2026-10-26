import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.job_event_bus import EventType
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.services.orchestration.message_stream_runtime import (
    MessageStreamRuntime,
)
from app.services.orchestration.trace_observer import MessageStreamTraceObserver


@pytest.fixture
def tool_failure_runtime_case():
    writer = MagicMock()
    writer.commit = AsyncMock()
    sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=sink,
        tool_result_sink=tool_result_sink,
    )
    return SimpleNamespace(
        writer=writer,
        sink=sink,
        tool_result_sink=tool_result_sink,
        runtime=runtime,
    )


async def register_failure_calls(case):
    await case.runtime.start_model("original-model", "primary")
    await case.runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[
        {"index": 0, "id": "target", "name": "read_context", "args": "{}"},
        {"index": 1, "id": "sibling", "name": "wait_for_session", "args": "{}"},
    ]))
    await case.runtime.finish_model()
    case.sink.reset_mock()
    case.tool_result_sink.reset_mock()
    case.writer.commit.reset_mock()


@pytest.mark.parametrize("request_first", [False, True])
@pytest.mark.parametrize("status", ["success", "error"])
async def test_tool_completion_preserves_blocks_and_artifact_but_stream_keeps_text(
    tool_failure_runtime_case, request_first, status,
):
    case = tool_failure_runtime_case
    await register_failure_calls(case)
    call_id = case.runtime.claim_tool_call_id("original-model", "target")
    await case.runtime.start_tool(tool_execution_id="native-target", tool_call_id=call_id, tool_name="read_context")
    message = ToolMessage(
        content=[{"type": "text", "text": "媒体回执"}, {"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"}],
        artifact={"structured_content": {"result": 3}}, status=status,
        tool_call_id="target", name="read_context",
    )

    async def stream_completion():
        await case.runtime.complete_tool(
            tool_execution_id="native-target", tool_call_id=call_id, tool_name="read_context",
            status="succeeded" if status == "success" else "failed", result=message.content, artifact=message.artifact,
        )

    async def request_completion():
        await case.runtime.complete_tool_from_message(message, model_call_id="original-model")

    first, second = (request_completion, stream_completion) if request_first else (stream_completion, request_completion)
    await first()
    await second()
    case.tool_result_sink.assert_awaited_once()
    completion, = case.tool_result_sink.await_args.args
    assert completion.content == message.content and completion.artifact == message.artifact
    assert completion.tool_outcome == ("success" if status == "success" else "failure")
    completed = [call.args[1] for call in case.writer.commit.await_args_list if call.args[0] == "tool.completed"]
    assert len(completed) == 1 and completed[0]["result"] == "媒体回执"


@pytest.mark.asyncio
async def test_failure_commits_exact_active_attempt_once_before_return(tool_failure_runtime_case):
    case = tool_failure_runtime_case
    await register_failure_calls(case)
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    async def commit(_completion: ToolResultCompletion):
        commit_started.set()
        await release_commit.wait()
        return SimpleNamespace(payload={"content": _completion.content, "tool_outcome": _completion.tool_outcome})

    case.tool_result_sink.side_effect = commit
    sibling = case.runtime.claim_tool_call_id("original-model", "sibling")
    await case.runtime.start_tool(tool_execution_id="native-sibling", tool_call_id=sibling, tool_name="wait_for_session")
    target = case.runtime.claim_tool_call_id("original-model", "target")
    await case.runtime.start_tool(tool_execution_id="native-target", tool_call_id=target, tool_name="read_context")
    task = asyncio.create_task(case.runtime.fail_tool_invocation(
        "original-model", "target", error="target-deleted",
    ))
    await asyncio.wait_for(commit_started.wait(), 1)
    assert not task.done()
    assert not [call for call in case.writer.commit.await_args_list if call.args[0] == "tool.completed"]
    release_commit.set()
    await task
    await case.runtime.fail_tool_invocation("original-model", "target", error="target-deleted")
    case.tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = case.tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "original-model"
    assert completion.tool_call_id == target
    assert completion.tool_invocation_id == "tool-invocation:original-model:target"
    assert completion.tool_attempt_id == "native-target"
    assert completion.tool_name == "read_context"
    assert completion.content == "target-deleted"
    # handler 抛错时 canonical 结果保留未知，运行镜像用 execution_error 说明失败归属。
    assert completion.tool_outcome == "unknown"
    target_events = [
        call
        for call in case.writer.commit.await_args_list
        if call.args[0] == "tool.completed"
        and call.args[1]["tool_call_id"] == target
    ]
    assert len(target_events) == 1
    target_event = target_events[0]
    assert target_event.args[1]["tool_execution_id"] == "native-target"
    assert target_event.args[1]["status"] == "failed"
    assert target_event.args[1]["outcome"] == "outcome_unknown"
    assert target_event.args[1]["completion_reason"] == "execution_error"
    assert target_event.args[1]["error"] == "target-deleted"
    assert target_event.kwargs["model_call_id"] == "original-model"
    assert target_event.kwargs["tool_invocation_id"] == (
        "tool-invocation:original-model:target"
    )
    await case.runtime.complete_tool(
        tool_execution_id="native-sibling", tool_call_id=sibling, tool_name="wait_for_session",
        status="succeeded", result="原 sibling 完成",
    )
    assert case.tool_result_sink.await_count == 2
    sibling_completion: ToolResultCompletion = (
        case.tool_result_sink.await_args_list[1].args[0]
    )
    assert sibling_completion.model_call_id == "original-model"
    assert sibling_completion.tool_call_id == sibling
    assert sibling_completion.tool_invocation_id == (
        "tool-invocation:original-model:sibling"
    )
    assert sibling_completion.tool_attempt_id == "native-sibling"
    assert sibling_completion.tool_name == "wait_for_session"
    assert sibling_completion.content == "原 sibling 完成"
    assert sibling_completion.tool_outcome == "success"
    completed_events = [
        call
        for call in case.writer.commit.await_args_list
        if call.args[0] == "tool.completed"
    ]
    assert len(completed_events) == 2
    sibling_event = next(
        call
        for call in completed_events
        if call.args[1]["tool_call_id"] == sibling
    )
    assert sibling_event.args[1]["tool_execution_id"] == "native-sibling"
    assert sibling_event.args[1]["status"] == "completed"
    assert sibling_event.args[1]["outcome"] == "success"
    assert sibling_event.kwargs["model_call_id"] == "original-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["target", "missing"])
async def test_failure_without_active_attempt_does_not_wait_or_create_result(
    tool_failure_runtime_case, provider_id: str,
):
    case = tool_failure_runtime_case
    await register_failure_calls(case)
    sibling = case.runtime.claim_tool_call_id("original-model", "sibling")
    await case.runtime.start_tool(
        tool_execution_id="native-sibling", tool_call_id=sibling, tool_name="wait_for_session",
    )
    case.writer.commit.reset_mock()

    await asyncio.wait_for(case.runtime.fail_tool_invocation(
        "original-model", provider_id, error="工具开始回调拒绝",
    ), timeout=1)

    assert case.runtime._pending_tool_starts == {}
    assert case.runtime._active_tool_executions == {
        "native-sibling": (sibling, "wait_for_session"),
    }
    case.tool_result_sink.assert_not_awaited()
    case.sink.assert_not_awaited()
    case.writer.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_rejects_multiple_active_attempts(tool_failure_runtime_case):
    case = tool_failure_runtime_case
    await register_failure_calls(case)
    target = case.runtime.claim_tool_call_id("original-model", "target")
    for execution_id in ("first-attempt", "second-attempt"):
        await case.runtime.start_tool(
            tool_execution_id=execution_id, tool_call_id=target, tool_name="read_context",
        )
    case.writer.commit.reset_mock()

    with pytest.raises(RuntimeError, match="多个 active attempt"):
        await case.runtime.fail_tool_invocation(
            "original-model", "target", error="工具执行失败",
        )

    assert case.runtime._pending_tool_starts == {}
    case.tool_result_sink.assert_not_awaited()
    case.writer.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_canonical_commit_failure_leaves_completion_unpublished(tool_failure_runtime_case, status):
    case = tool_failure_runtime_case
    await register_failure_calls(case)
    call_id = case.runtime.claim_tool_call_id("original-model", "target")
    await case.runtime.start_tool(tool_execution_id="native-target", tool_call_id=call_id, tool_name="read_context")
    case.tool_result_sink.side_effect = RuntimeError("原 canonical 提交失败")
    values = {"tool_execution_id": "native-target", "tool_call_id": call_id,
              "tool_name": "read_context", "status": status, "result": "原结果"}
    with pytest.raises(RuntimeError, match="原 canonical 提交失败"):
        await case.runtime.complete_tool(**values)
    assert not [call for call in case.writer.commit.await_args_list if call.args[0] == "tool.completed"]
    case.tool_result_sink.side_effect = None
    await case.runtime.complete_tool(**values)
    await case.runtime.complete_tool(**values)
    assert len([call for call in case.writer.commit.await_args_list if call.args[0] == "tool.completed"]) == 1
    completions = [call.args[0] for call in case.tool_result_sink.await_args_list]
    assert all(isinstance(completion, ToolResultCompletion) for completion in completions)
    assert [completion.item_id for completion in completions] == [
        f"item-native-target-result-{call_id}", f"item-native-target-result-{call_id}",
    ]
    assert all(completion.model_call_id == "original-model" for completion in completions)
    assert all(completion.tool_call_id == call_id for completion in completions)
    assert all(completion.tool_invocation_id == "tool-invocation:original-model:target" for completion in completions)
    assert all(completion.tool_attempt_id == "native-target" for completion in completions)
    assert all(completion.tool_name == "read_context" for completion in completions)
    assert all(completion.content == "原结果" for completion in completions)
    assert all(completion.tool_outcome == ("success" if status == "succeeded" else "failure") for completion in completions)


@pytest.mark.asyncio
async def test_model_registration_failure_does_not_publish_unregistered_lifecycle() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    registrar = AsyncMock(side_effect=RuntimeError("登记失败"))
    runtime = MessageStreamRuntime(writer, model_call_registrar=registrar)
    preparation = asyncio.create_task(runtime.prepare_tool_execution("model_unregistered"))
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="登记失败"):
        await runtime.start_model("model_unregistered", "primary")
    with pytest.raises(RuntimeError, match="登记失败"):
        await preparation

    assert runtime.current_model_call_id is None
    assert runtime.current_attempt == 0
    writer.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_execution_waits_for_emitting_model_and_commits_exact_calls() -> None:
    canonical_sink = AsyncMock()
    registrar = AsyncMock(return_value="execution-tool-boundary")
    runtime = MessageStreamRuntime(
        None, canonical_item_sink=canonical_sink, model_call_registrar=registrar,
    )
    await runtime.accept_message_chunk(AIMessageChunk(
        content="", tool_call_chunks=[{
            "index": 0, "id": "call_boundary", "name": "skill_load", "args": "{}",
        }],
    ), model_call_id="model_boundary")
    preparation = asyncio.create_task(runtime.prepare_tool_execution("model_boundary"))
    await asyncio.sleep(0)
    assert not preparation.done()
    canonical_sink.assert_not_awaited()

    await runtime.start_model("model_boundary", "primary")
    await preparation
    canonical_sink.assert_awaited_once()
    item, = canonical_sink.await_args.args[0]
    assert item.payload["tool_call_id"] == "model_boundary:tool-call:call_boundary"
    assert item.projection_identity.model_call_id == "model_boundary"
    await runtime.prepare_tool_execution("model_boundary")
    canonical_sink.assert_awaited_once()

    await runtime.start_model("model_next", "primary")
    with pytest.raises(RuntimeError, match="emitting model-call.*不一致"):
        await runtime.prepare_tool_execution("model_boundary")
    canonical_sink.assert_awaited_once()


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
async def test_provider_fallback_marks_previous_model_attempt_failed() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "backup_3")
    await runtime.fail_model(
        code="BadRequestError",
        message="tool call/result 不匹配",
    )
    await runtime.start_model("model_2", "backup_4")

    events = [call.args for call in writer.commit.await_args_list]
    assert [event[0] for event in events] == [
        "model.started",
        "model.failed",
        "model.started",
    ]
    assert events[1][1]["model_call_id"] == "model_1"
    assert events[1][1]["outcome"] == "upstream_error"
    assert events[2][1]["model_call_id"] == "model_2"


@pytest.mark.asyncio
async def test_internal_provider_stream_persists_turnless_items_without_writer() -> None:
    canonical_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    registrar = AsyncMock(return_value="execution-internal-stream")
    outcome_sink = AsyncMock()
    observer = AsyncMock()
    runtime = MessageStreamRuntime(
        None,
        normalized_block_observer=observer,
        canonical_item_sink=canonical_sink,
        tool_result_sink=tool_result_sink,
        canonical_turn_id=None,
        model_call_registrar=registrar,
        model_call_outcome_sink=outcome_sink,
    )

    await runtime.start_model("model_internal", "provider")
    await runtime.accept_message_chunk(AIMessageChunk(content="internal output"))
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_internal",
                    "name": "read_file",
                    "args": "{}",
                }
            ],
        )
    )
    await runtime.finish_model()
    tool_call_id = runtime.claim_tool_call_id("model_internal", "call_internal")
    assert tool_call_id == "model_internal:tool-call:call_internal"
    await runtime.start_tool(
        tool_execution_id="run_internal",
        tool_call_id=tool_call_id,
        tool_name="read_file",
    )
    await runtime.complete_tool(
        tool_execution_id="run_internal",
        tool_call_id=tool_call_id,
        tool_name="read_file",
        status="succeeded",
        result="internal file contents",
    )
    await runtime.complete_model(outcome="accepted")

    registrar.assert_awaited_once_with("model_internal", 1, "provider")
    outcome_sink.assert_awaited_once_with("model_internal", "completed")
    canonical_sink.assert_awaited_once()
    (model_items,) = canonical_sink.await_args_list[0].args
    assert {item.semantic_kind for item in model_items} == {
        "assistant_output",
        "tool_call",
    }
    for item in model_items:
        assert item.turn_id is None
        assert item.turn_scope is None
        assert item.item_id.startswith("item-execution-internal-stream-model-model_internal-block-")
    (assistant_item,) = [
        item for item in model_items if item.semantic_kind == "assistant_output"
    ]
    (tool_call_item,) = [
        item for item in model_items if item.semantic_kind == "tool_call"
    ]
    assert assistant_item.payload == "internal output"
    assert tool_call_item.payload["tool_call_id"] == tool_call_id
    assert tool_call_item.payload["name"] == "read_file"
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model_internal"
    assert completion.tool_call_id == tool_call_item.payload["tool_call_id"]
    assert completion.tool_invocation_id == tool_call_item.payload["tool_invocation_id"]
    assert completion.tool_attempt_id == "run_internal"
    assert completion.tool_name == "read_file"
    assert completion.content == "internal file contents"
    assert completion.tool_outcome == "success"
    assert runtime.writer is None
    assert runtime.activities is None
    observer.assert_awaited()


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
                    "name": "invoke_extension_tool",
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
    assert first_payload["tool_name"] == "invoke_extension_tool"
    assert second_payload["tool_call_id"] == "model_1:tool-call:call_1"
    assert second_payload["tool_name"] == "invoke_extension_tool"
    assert second_payload["arguments"] == {"tool_name": "unknown_tool"}
    assert second_payload["arguments_complete"] is True
    second_event = writer.commit.await_args_list[2]
    assert second_event.kwargs["tool_call_id"] == "model_1:tool-call:call_1"
    assert second_event.kwargs["tool_invocation_id"] == (
        "tool-invocation:model_1:call_1"
    )
    assert runtime.claim_tool_call_id("model_1", "call_1") == (
        "model_1:tool-call:call_1"
    )
    with pytest.raises(RuntimeError, match="工具调用已经认领"):
        runtime.claim_tool_call_id("model_1", "call_1")
    with pytest.raises(RuntimeError, match="缺少原 emitting ModelCall/ToolCall"):
        runtime.claim_tool_call_id("missing-model", "call_1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_args", "include_args", "expected_arguments", "expected_complete"),
    [
        pytest.param("{}", True, {}, True, id="empty-object-json"),
        pytest.param({}, True, {}, True, id="empty-object-mapping"),
        pytest.param(
            '{"raw":"value"}', True, {"raw": "value"}, True, id="raw-field-json"
        ),
        pytest.param(
            {"raw": "value"}, True, {"raw": "value"}, True, id="raw-field-mapping"
        ),
        pytest.param(None, False, {}, False, id="missing-arguments"),
        pytest.param("", True, {}, False, id="empty-string"),
        pytest.param(
            '{"path":', True, {"raw": '{"path":'}, False, id="unclosed-json"
        ),
        pytest.param(
            '[]', True, {"raw": "[]"}, False, id="non-object-json"
        ),
    ],
)
async def test_tool_call_argument_completeness_requires_a_received_object(
    raw_args: object,
    include_args: bool,
    expected_arguments: dict[str, object],
    expected_complete: bool,
) -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)
    await runtime.start_model("model_arguments", "primary")

    tool_call_chunk: dict[str, object] = {
        "index": 0,
        "id": "call_arguments",
        "name": "read_file",
    }
    if include_args:
        tool_call_chunk["args"] = raw_args
    await runtime.accept_message_chunk(
        SimpleNamespace(content="", tool_call_chunks=[tool_call_chunk])
    )

    call_id = "model_arguments:tool-call:call_arguments"
    delta = next(
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.delta"
    )
    assert delta["arguments"] == expected_arguments
    assert delta["arguments_complete"] is expected_complete
    assert runtime.pending_tool_calls() == (
        (call_id, "read_file", expected_complete),
    )
    if expected_complete:
        assert runtime.claim_tool_call_id("model_arguments", "call_arguments") == call_id
        assert runtime.pending_tool_calls() == ()


@pytest.mark.asyncio
async def test_parallel_tool_calls_with_reused_provider_index_get_distinct_identities() -> None:
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
                    "id": "call_first",
                    "name": "grep",
                    "args": '{"pattern":"first"}',
                }
            ],
        )
    )
    # GLM-5.3 的兼容 OpenAI 流在并行调用时可能再次发送 index=0，
    # 但 provider id 已经表明这是同一模型调用中的第二个 call。
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_second",
                    "name": "apply_patch",
                    "args": '{"input":"patch"}',
                }
            ],
        )
    )

    assert runtime._tool_call_order == [
        "model_1:tool-call:call_first",
        "model_1:tool-call:call_second",
    ]
    deltas = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.delta"
    ]
    assert [delta["tool_call_id"] for delta in deltas] == runtime._tool_call_order
    assert [delta["arguments"] for delta in deltas] == [
        {"pattern": "first"},
        {"input": "patch"},
    ]


@pytest.mark.asyncio
async def test_same_name_and_argument_tool_calls_are_claimed_by_exact_provider_id() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    for index, path in enumerate(("a.txt", "a.txt")):
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

    assert runtime.claim_tool_call_id("model_1", "call_1") == (
        "model_1:tool-call:call_1"
    )
    assert runtime.claim_tool_call_id("model_1", "call_0") == (
        "model_1:tool-call:call_0"
    )


@pytest.mark.asyncio
async def test_next_model_start_preserves_late_tool_start_correlation() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "index": 0,
                "id": "call_from_model_1",
                "name": "read_file",
                "args": '{"path":"README.md"}',
            }],
        )
    )

    # LangGraph 可以在父 astream_events 消费 on_tool_start 前就开始下一次模型调用。
    await runtime.finish_model()
    await runtime.start_model("model_2", "primary")

    assert runtime.claim_tool_call_id(
        "model_1", "call_from_model_1",
    ) == "model_1:tool-call:call_from_model_1"
    await runtime.start_tool(
        tool_execution_id="tool_execution_1",
        tool_call_id="model_1:tool-call:call_from_model_1",
        tool_name="read_file",
    )
    assert runtime.pending_tool_calls() == ()
    tool_events = [
        call
        for call in writer.commit.await_args_list
        if call.args[0] in {"tool_call.completed", "tool.started"}
    ]
    assert [call.kwargs["model_call_id"] for call in tool_events] == [
        "model_1",
        "model_1",
    ]
    assert [call.kwargs["tool_call_id"] for call in tool_events] == [
        "model_1:tool-call:call_from_model_1",
        "model_1:tool-call:call_from_model_1",
    ]
    assert [call.kwargs["tool_invocation_id"] for call in tool_events] == [
        "tool-invocation:model_1:call_from_model_1",
        "tool-invocation:model_1:call_from_model_1",
    ]


@pytest.mark.asyncio
async def test_pending_failure_keeps_emitting_owner_after_next_model() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(AIMessageChunk(
        content="",
        tool_call_chunks=[{
            "index": 0,
            "id": "call_pending",
            "name": "read_file",
            "args": '{"path":"README.md"}',
        }],
    ))
    await runtime.finish_model()
    await runtime.start_model("model_2", "primary")

    await runtime.fail_pending_tool_calls(
        completion_reason="tool_dispatch_timeout",
        error="分派超时",
    )

    completion = next(
        call for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    )
    assert completion.kwargs["model_call_id"] == "model_1"
    assert completion.kwargs["tool_call_id"] == "model_1:tool-call:call_pending"
    assert completion.kwargs["tool_invocation_id"] == (
        "tool-invocation:model_1:call_pending"
    )
    assert completion.args[1]["completion_reason"] == "tool_dispatch_timeout"


@pytest.mark.asyncio
async def test_registered_missing_tool_owner_is_not_replaced_by_current_model() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.accept_message_chunk(AIMessageChunk(
        content="",
        tool_call_chunks=[{
            "index": 0,
            "name": "read_file",
            "args": '{"path":"README.md"}',
        }],
    ))
    tool_call_id = runtime._tool_call_order[0]
    assert runtime._tool_call_model_call_ids[tool_call_id] is None

    await runtime.start_model("current-model", "primary")
    await runtime.start_tool(
        tool_execution_id="unbound_execution",
        tool_call_id=tool_call_id,
        tool_name="read_file",
    )
    await runtime.complete_tool(
        tool_execution_id="unbound_execution",
        tool_call_id=tool_call_id,
        tool_name="read_file",
        status="succeeded",
        result="README 内容",
    )

    tool_events = [
        call
        for call in writer.commit.await_args_list
        if call.args[0] in {"tool_call.completed", "tool.started", "tool.completed"}
    ]
    assert [call.kwargs["model_call_id"] for call in tool_events] == [
        None,
        None,
        None,
    ]
    assert all(call.kwargs["tool_call_id"] == tool_call_id for call in tool_events)
    assert all(
        call.kwargs["tool_invocation_id"].startswith(
            "tool-invocation:unknown-model-call:"
        )
        for call in tool_events
    )


@pytest.mark.asyncio
async def test_interrupt_closes_calls_under_emitting_model_owner() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    for model_call_id, provider_call_id in (
        ("model_1", "call_first"),
        ("model_2", "call_current"),
    ):
        await runtime.start_model(model_call_id, "primary")
        await runtime.accept_message_chunk(AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "index": 0,
                "id": provider_call_id,
                "name": "read_file",
                "args": '{"path":"README.md"}',
            }],
        ))
        await runtime.finish_model()
    await runtime.start_model("model_3", "primary")

    await runtime.finalize_interruption_facts()

    closures = {
        call.kwargs["tool_call_id"]: call
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
        and call.args[1].get("completion_reason") == "user_interrupt"
    }
    first = closures["model_1:tool-call:call_first"]
    current = closures["model_2:tool-call:call_current"]
    assert first.kwargs["model_call_id"] == "model_1"
    assert first.kwargs["tool_invocation_id"] == (
        "tool-invocation:model_1:call_first"
    )
    assert current.kwargs["model_call_id"] == "model_2"
    assert current.kwargs["tool_invocation_id"] == (
        "tool-invocation:model_2:call_current"
    )


@pytest.mark.asyncio
async def test_reused_provider_tool_call_id_is_scoped_per_model_call() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    for model_call_id in ("model_1", "model_2"):
        await runtime.start_model(model_call_id, "primary")
        await runtime.accept_message_chunk(
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "index": 0,
                        "id": "call_reused",
                        "name": "read_file",
                        "args": '{"path":"README.md"}',
                    }
                ],
            )
        )
        await runtime.finish_model()

    assert runtime._tool_call_order == [
        "model_1:tool-call:call_reused",
        "model_2:tool-call:call_reused",
    ]
    assert runtime._tool_invocation_id_for(
        "model_1:tool-call:call_reused"
    ) == "tool-invocation:model_1:call_reused"
    assert runtime._tool_invocation_id_for(
        "model_2:tool-call:call_reused"
    ) == "tool-invocation:model_2:call_reused"
    assert runtime.claim_tool_call_id("model_2", "call_reused") == (
        "model_2:tool-call:call_reused"
    )
    assert runtime.claim_tool_call_id("model_1", "call_reused") == (
        "model_1:tool-call:call_reused"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_completed", [False, True])
async def test_historical_tool_message_does_not_complete_reused_call_in_next_model(
    first_completed: bool,
) -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)
    for ordinal in (1, 2):
        model_call_id = f"model_{ordinal}"
        await runtime.start_model(model_call_id, "primary")
        await runtime.accept_message_chunk(AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "index": 0, "id": "call_reused", "name": "read_file",
                "args": '{"path":"README.md"}',
            }],
        ))
        await runtime.finish_model()
        await runtime.start_tool(
            tool_execution_id=f"execution_{ordinal}",
            tool_call_id=f"{model_call_id}:tool-call:call_reused",
            tool_name="read_file",
        )
        if ordinal == 1 and first_completed:
            await runtime.complete_tool(
                tool_execution_id="execution_1", tool_call_id="model_1:tool-call:call_reused",
                tool_name="read_file", status="succeeded", result="旧调用结果",
            )
    old = ToolMessage(
        content="旧调用结果", tool_call_id="call_reused", name="read_file",
        response_metadata={"model_call_id": "model_1"},
    )
    await runtime.complete_tool_from_message(old, model_call_id="model_1")
    assert runtime._active_tool_executions == {
        "execution_2": ("model_2:tool-call:call_reused", "read_file"),
    }
    completed = [call.args[1] for call in writer.commit.await_args_list if call.args[0] == "tool.completed"]
    assert [(event["tool_call_id"], event["result"]) for event in completed] == [
        ("model_1:tool-call:call_reused", "旧调用结果"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_status", "expected_status", "stream_outcome", "canonical_outcome", "completion_reason"),
    [
        ("success", "completed", "success", "success", "tool_completed"),
        ("error", "failed", "provider_error", "failure", "provider_failed"),
    ],
)
async def test_tool_message_waits_for_real_start_before_persisting_terminal_event(
    tool_failure_runtime_case,
    message_status: str,
    expected_status: str,
    stream_outcome: str,
    canonical_outcome: str,
    completion_reason: str,
) -> None:
    writer = tool_failure_runtime_case.writer
    canonical_item_sink = tool_failure_runtime_case.sink
    tool_result_sink = tool_failure_runtime_case.tool_result_sink
    runtime = tool_failure_runtime_case.runtime

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_late",
                    "name": "read_file",
                    "args": '{"path":"README.md"}',
                }
            ],
        )
    )

    reconcile = asyncio.create_task(
        runtime.complete_tool_from_message(
            ToolMessage(
                content="README 内容",
                tool_call_id="call_late",
                name="read_file",
                status=message_status,
            ),
            model_call_id="model_1",
        )
    )
    await asyncio.sleep(0)
    assert not reconcile.done()

    normalized_call_id = "model_1:tool-call:call_late"
    assert runtime.claim_tool_call_id(
        "model_1", "call_late",
    ) == normalized_call_id
    await runtime.start_tool(
        tool_execution_id="execution_a",
        tool_call_id=normalized_call_id,
        tool_name="read_file",
    )
    await reconcile

    # LangGraph 的 on_tool_end 之后到达时只会重放同一真实 attempt。
    await runtime.complete_tool(
        tool_execution_id="execution_a",
        tool_call_id=normalized_call_id,
        tool_name="read_file",
        status="succeeded" if message_status == "success" else "failed",
        result="README 内容",
        error="README 内容" if message_status == "error" else None,
        outcome=stream_outcome,
    )

    assert runtime._active_tool_executions == {}
    assert normalized_call_id in runtime._completed_tool_call_ids
    canonical_item_sink.assert_not_awaited()
    tool_result_sink.assert_awaited_once()
    result_completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert result_completion.model_call_id == "model_1"
    assert result_completion.tool_call_id == normalized_call_id
    assert result_completion.tool_invocation_id == (
        "tool-invocation:model_1:call_late"
    )
    assert result_completion.tool_attempt_id == "execution_a"
    assert result_completion.content == "README 内容"
    assert result_completion.tool_outcome == canonical_outcome
    tool_call_completions = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    ]
    assert len(tool_call_completions) == 1
    assert tool_call_completions[0]["status"] == "completed"
    assert tool_call_completions[0]["completion_reason"] == "tool_started"
    assert tool_call_completions[0]["arguments_complete"] is True
    completion_events = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool.completed"
    ]
    assert len(completion_events) == 1
    assert completion_events[0]["tool_execution_id"] == "execution_a"
    assert completion_events[0]["tool_attempt_id"] == "execution_a"
    assert completion_events[0]["status"] == expected_status
    assert completion_events[0]["outcome"] == stream_outcome
    assert completion_events[0]["result"] == "README 内容"
    assert completion_events[0]["completion_reason"] == completion_reason
    if message_status == "error":
        assert completion_events[0]["error"] == "README 内容"
    # 真实 producer 的完整 payload 必须能通过公开 SSE 使用的严格 codec。
    public_event = message_stream_to_json(message_stream_to_proto({
        "event_id": "event_tool_message_completion",
        "session_id": "ses_00000000400070008000000000000099",
        "thread_id": "thr_00000000700070008000000000000001",
        "turn_id": "turn_tool_message_completion",
        "turn_stream_id": "stream_tool_message_completion",
        "event_seq": 1,
        "type": "tool.completed",
        "payload": completion_events[0],
    }))
    assert public_event["payload"]["status"] == expected_status
    assert public_event["payload"]["outcome"] == stream_outcome
    assert [
        call.args[0]
        for call in writer.commit.await_args_list
        if call.args[0] in {"tool_call.completed", "tool.started", "tool.completed"}
    ] == ["tool_call.completed", "tool.started", "tool.completed"]


@pytest.mark.asyncio
async def test_cancelled_tool_message_waiter_preserves_shared_start_for_remaining_waiter() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    canonical_item_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=canonical_item_sink,
        tool_result_sink=tool_result_sink,
    )

    await runtime.start_model("model_shared_waiter", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_shared_waiter",
                    "name": "read_file",
                    "args": '{"path":"README.md"}',
                }
            ],
        )
    )
    message = ToolMessage(
        content="共享结果",
        tool_call_id="call_shared_waiter",
        name="read_file",
    )
    first_waiter = asyncio.create_task(
        runtime.complete_tool_from_message(
            message,
            model_call_id="model_shared_waiter",
        )
    )
    remaining_waiter = asyncio.create_task(
        runtime.complete_tool_from_message(
            message,
            model_call_id="model_shared_waiter",
        )
    )
    await asyncio.sleep(0)

    call_id = "model_shared_waiter:tool-call:call_shared_waiter"
    pending_start = runtime._pending_tool_starts.get(call_id)
    assert pending_start is not None
    assert not first_waiter.done()
    assert not remaining_waiter.done()

    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    try:
        assert runtime._pending_tool_starts.get(call_id) is pending_start
        assert not pending_start.done()
        assert not remaining_waiter.done()
        tool_result_sink.assert_not_awaited()

        claimed_call_id = runtime.claim_tool_call_id(
            "model_shared_waiter", "call_shared_waiter",
        )
        assert claimed_call_id == call_id
        await runtime.start_tool(
            tool_execution_id="execution_a",
            tool_call_id=claimed_call_id,
            tool_name="read_file",
        )
        assert call_id not in runtime._pending_tool_starts
        await asyncio.wait_for(remaining_waiter, timeout=1)
    finally:
        if not remaining_waiter.done():
            remaining_waiter.cancel()
        await asyncio.gather(remaining_waiter, return_exceptions=True)

    canonical_item_sink.assert_not_awaited()
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model_shared_waiter"
    assert completion.tool_call_id == call_id
    assert completion.tool_invocation_id == (
        "tool-invocation:model_shared_waiter:call_shared_waiter"
    )
    assert completion.tool_attempt_id == "execution_a"
    assert completion.content == "共享结果"
    assert completion.tool_outcome == "success"
    assert [
        call.args[0]
        for call in writer.commit.await_args_list
        if call.args[0] in {"tool_call.completed", "tool.started", "tool.completed"}
    ] == ["tool_call.completed", "tool.started", "tool.completed"]


@pytest.mark.asyncio
async def test_tool_message_closes_call_without_inventing_complete_arguments() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    runtime = MessageStreamRuntime(writer)

    await runtime.start_model("model_incomplete_call", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_incomplete",
                    "name": "read_file",
                    "args": '{"path":',
                }
            ],
        )
    )
    call_id = "model_incomplete_call:tool-call:call_incomplete"
    reconcile = asyncio.create_task(
        runtime.complete_tool_from_message(
            ToolMessage(
                content="README 内容",
                tool_call_id="call_incomplete",
                name="read_file",
            ),
            model_call_id="model_incomplete_call",
        )
    )
    await asyncio.sleep(0)
    assert not reconcile.done()
    assert runtime.claim_tool_call_id("model_incomplete_call", "call_incomplete") == call_id
    await runtime.start_tool(
        tool_execution_id="execution_incomplete",
        tool_call_id=call_id,
        tool_name="read_file",
    )
    await reconcile

    await runtime.finalize_interruption_facts()

    closures = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    ]
    assert len(closures) == 1
    assert closures[0]["status"] == "completed"
    assert closures[0]["arguments_complete"] is False
    assert closures[0]["completion_reason"] == "tool_started"
    assert call_id in runtime._completed_tool_call_ids


@pytest.mark.asyncio
async def test_tool_message_waits_for_its_start_before_claiming_same_argument_calls() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    canonical_item_sink = AsyncMock()
    tool_result_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=canonical_item_sink,
        tool_result_sink=tool_result_sink,
    )

    # 两次同名同参调用：第一次 ToolMessage 先进入请求边界，必须等到
    # 第一次真实 start，再允许后续调用按原顺序被认领。
    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_first",
                    "name": "read_file",
                    "args": '{"path":"a.txt"}',
                }
            ],
        )
    )
    await runtime.start_model("model_2", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_second",
                    "name": "read_file",
                    "args": '{"path":"a.txt"}',
                }
            ],
        )
    )

    canonical_calls_before_reconcile = tuple(canonical_item_sink.await_args_list)
    assert len(canonical_calls_before_reconcile) == 1
    (canonical_tool_call,) = canonical_calls_before_reconcile[0].args[0]
    assert canonical_tool_call.semantic_kind == "tool_call"
    assert canonical_tool_call.projection_identity.model_call_id == "model_1"
    assert canonical_tool_call.payload["tool_call_id"] == (
        "model_1:tool-call:call_first"
    )

    reconcile = asyncio.create_task(
        runtime.complete_tool_from_message(
            ToolMessage(
                content="第一次结果",
                tool_call_id="call_first",
                name="read_file",
            ),
            model_call_id="model_1",
        )
    )
    await asyncio.sleep(0)
    assert not reconcile.done()

    first_claim = runtime.claim_tool_call_id("model_1", "call_first")
    assert first_claim == "model_1:tool-call:call_first"
    await runtime.start_tool(
        tool_execution_id="execution_first",
        tool_call_id=first_claim,
        tool_name="read_file",
    )
    await reconcile
    assert canonical_item_sink.await_count == len(canonical_calls_before_reconcile)
    assert tuple(canonical_item_sink.await_args_list) == canonical_calls_before_reconcile
    assert all(
        item.semantic_kind != "tool_result"
        for sink_call in canonical_item_sink.await_args_list
        for item in sink_call.args[0]
    )

    second_call_id = "model_2:tool-call:call_second"
    assert runtime.pending_tool_calls() == (
        (second_call_id, "read_file", True),
    )
    second_claim = runtime.claim_tool_call_id("model_2", "call_second")
    assert second_claim == second_call_id
    assert runtime.pending_tool_calls() == ()
    starts = [
        call.args[1]
        for call in writer.commit.await_args_list
        if call.args[0] == "tool.started"
    ]
    assert [(event["tool_execution_id"], event["tool_call_id"]) for event in starts] == [
        ("execution_first", first_claim),
    ]
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model_1"
    assert completion.tool_call_id == first_claim
    assert completion.tool_invocation_id == "tool-invocation:model_1:call_first"
    assert completion.tool_attempt_id == "execution_first"
    assert completion.content == "第一次结果"
    assert completion.tool_outcome == "success"
    assert runtime._completed_tool_result_call_ids == {first_claim}
    assert runtime._completed_tool_result_execution_by_call_id == {
        first_claim: "execution_first",
    }
    tool_call_completions = [
        call
        for call in writer.commit.await_args_list
        if call.args[0] == "tool_call.completed"
    ]
    assert len(tool_call_completions) == 1
    assert tool_call_completions[0].kwargs["model_call_id"] == "model_1"
    assert tool_call_completions[0].kwargs["tool_call_id"] == first_claim
    assert tool_call_completions[0].args[1]["completion_reason"] == "tool_started"
    completed = [
        call
        for call in writer.commit.await_args_list
        if call.args[0] == "tool.completed"
    ]
    assert len(completed) == 1
    assert completed[0].kwargs["model_call_id"] == "model_1"
    assert completed[0].kwargs["tool_call_id"] == first_claim
    assert completed[0].kwargs["tool_invocation_id"] == (
        "tool-invocation:model_1:call_first"
    )
    assert completed[0].kwargs["tool_attempt_id"] == "execution_first"
    assert completed[0].args[1]["tool_execution_id"] == "execution_first"
    assert completed[0].args[1]["result"] == "第一次结果"
    assert [
        call.args[0]
        for call in writer.commit.await_args_list
        if call.args[0] in {"tool_call.completed", "tool.started", "tool.completed"}
    ] == ["tool_call.completed", "tool.started", "tool.completed"]


@pytest.mark.asyncio
async def test_late_provider_delta_after_model_sealed_does_not_reappend_canonical_item() -> None:
    writer = MagicMock()
    writer.commit = AsyncMock()
    canonical_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        writer,
        canonical_item_sink=canonical_sink,
        canonical_turn_id="turn_1",
    )

    await runtime.start_model("model_1", "primary")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_late",
                    "name": "read_file",
                    "args": '{"path":"debug/tool_test_write.txt"}',
                }
            ],
        )
    )
    await runtime.finish_model()

    # provider 在 on_chat_model_end 之后重复投递同一调用的迟到参数片段。
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call_late",
                    "name": "read_file",
                    "args": "{\"file_path\":\"debug/tool_test_write.txt\"}",
                }
            ],
        )
    )

    canonical_sink.assert_awaited_once()
    assert runtime._sealed_canonical_model_call_ids == {"model_1"}


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
        "tool_call.completed",
        "tool.started",
        "tool.completed",
        "model.failed",
    ]
    # tool_call 到来会先闭合 reasoning block，属于 carrier 切换而非中断收尾。
    assert calls[3][1]["partial"] is False
    assert calls[5][1]["status"] == "completed"
    assert calls[5][1]["completion_reason"] == "tool_started"
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

@pytest.mark.asyncio
async def test_writerless_internal_failure_marks_model_call_failed() -> None:
    canonical_sink = AsyncMock()
    outcome_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        None,
        canonical_item_sink=canonical_sink,
        model_call_registrar=AsyncMock(return_value="execution-internal-failed"),
        model_call_outcome_sink=outcome_sink,
    )
    await runtime.start_model("model_internal_failed", "provider")
    await runtime.accept_message_chunk(AIMessageChunk(content="partial"))
    await runtime.fail_model(code="job_timeout", message="deadline")

    outcome_sink.assert_awaited_once_with("model_internal_failed", "failed")
    canonical_sink.assert_awaited_once()
    (items,) = canonical_sink.await_args.args
    assert items[0].status == "failed"
    assert items[0].turn_id is None
