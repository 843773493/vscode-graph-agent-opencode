from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.job_event_bus import EventType
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.services.orchestration.execution_step.failures import (
    handle_step_cancelled,
    handle_step_failure,
)
from app.services.orchestration.execution_step.runner import StepRunner
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


class _SnapshotConfigService:
    def get_snapshot(self) -> object:
        return "snapshot"

    def use_snapshot(self, snapshot: object):
        return nullcontext(snapshot)


@pytest.fixture
def step_runner() -> tuple[StepRunner, AsyncMock]:
    runner = StepRunner(
        ports=SimpleNamespace(config_service=_SnapshotConfigService()),
        execution_scope_registry=SimpleNamespace(),
    )
    delegated_run = AsyncMock(return_value="finished")
    runner._run_step_with_snapshot = delegated_run
    return runner, delegated_run


@pytest.fixture
def writerless_runtime() -> tuple[
    MessageStreamRuntime,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    AsyncMock,
]:
    canonical_sink = AsyncMock()
    tool_result_sink = AsyncMock(wraps=lambda completion: SimpleNamespace(payload={
        "content": completion.content, "tool_outcome": completion.tool_outcome,
    }))
    registrar = AsyncMock(return_value="execution_internal")
    outcome_sink = AsyncMock()
    runtime = MessageStreamRuntime(
        None,
        canonical_item_sink=canonical_sink,
        tool_result_sink=tool_result_sink,
        canonical_turn_id=None,
        model_call_registrar=registrar,
        model_call_outcome_sink=outcome_sink,
    )
    return runtime, canonical_sink, tool_result_sink, registrar, outcome_sink


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding", "job_id", "message_id"),
    [
        (
            UserExecutionIdentity(
                job_id="job_user",
                turn_id="turn_user",
                admission_idempotency_key="admission_user",
            ),
            "job_user",
            "message_user",
        ),
        (
            InternalExecutionIntent(
                message_id="message_internal",
                execution_id="execution_internal",
                admission_idempotency_key="admission_internal",
            ),
            "job_internal",
            "message_internal",
        ),
    ],
)
async def test_step_runner_forwards_execution_binding_without_collapsing_identity(
    step_runner: tuple[StepRunner, AsyncMock],
    binding: UserExecutionIdentity | InternalExecutionIntent,
    job_id: str,
    message_id: str,
) -> None:
    runner, delegated_run = step_runner
    result = await runner.run_step(
        "session",
        "request",
        thread_id="thread",
        job_id=job_id,
        message_id=message_id,
        message_created_at="2026-10-04T00:00:00+00:00",
        execution_binding=binding,
    )

    assert result == "finished"
    delegated_run.assert_awaited_once()
    call = delegated_run.await_args
    assert call.args == ("session", "request")
    assert call.kwargs["job_id"] == job_id
    assert call.kwargs["message_id"] == message_id
    assert call.kwargs["execution_binding"] is binding
    if isinstance(binding, UserExecutionIdentity):
        assert binding.job_id != binding.turn_id


@pytest.mark.asyncio
async def test_writerless_internal_failure_converges_canonical_model_and_execution(
    writerless_runtime: tuple[
        MessageStreamRuntime,
        AsyncMock,
        AsyncMock,
        AsyncMock,
        AsyncMock,
    ],
) -> None:
    runtime, canonical_sink, _tool_result_sink, _registrar, outcome_sink = writerless_runtime
    await runtime.start_model("model_internal_failure", "provider")
    await runtime.accept_message_chunk(
        AIMessageChunk(content="partial internal output")
    )

    checkpointer = MagicMock()
    publish = AsyncMock()
    await handle_step_failure(
        error=RuntimeError("internal execution failed"),
        interrupt_state=SimpleNamespace(
            interrupt_request_id=None,
            cancellation_reason=None,
            tool_name=None,
        ),
        message_stream_runtime=runtime,
        message_stream_writer=None,
        checkpointer=checkpointer,
        session_id="session",
        thread_id="thread",
        job_id="job_internal",
        turn_id=None,
        execution_id="execution_internal",
        checkpoint_ns="",
        publish=publish,
        logger=MagicMock(spec=logging.Logger),
    )

    persisted_items = [
        item
        for sink_call in canonical_sink.await_args_list
        for item in sink_call.args[0]
    ]
    (assistant_item,) = [
        item for item in persisted_items if item.semantic_kind == "assistant_output"
    ]
    assert assistant_item.status == "failed"
    assert assistant_item.turn_id is None
    assert assistant_item.item_id.startswith("item-execution_internal-model-model_internal_failure-block-")
    outcome_sink.assert_awaited_once_with("model_internal_failure", "failed")
    checkpointer.converge_execution.assert_called_once_with(
        "session",
        thread_id="thread",
        turn_id=None,
        execution_id="execution_internal",
        outcome="failed",
        turn_status=None,
        checkpoint_ns="",
    )
    checkpointer.mark_execution_lost.assert_not_called()
    publish.assert_awaited_once_with(
        EventType.ERROR,
        {"error": "internal execution failed", "phase": "agent_execution"},
    )


@pytest.mark.asyncio
async def test_writerless_internal_user_cancel_converges_tool_result_and_execution(
    writerless_runtime: tuple[
        MessageStreamRuntime,
        AsyncMock,
        AsyncMock,
        AsyncMock,
        AsyncMock,
    ],
) -> None:
    runtime, canonical_sink, tool_result_sink, _registrar, outcome_sink = writerless_runtime
    await runtime.start_model("model_internal_cancel", "provider")
    await runtime.accept_message_chunk(
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "provider_call",
                    "name": "read_file",
                    "args": "{}",
                }
            ],
        )
    )
    tool_call_id = runtime.claim_tool_call_id("model_internal_cancel", "provider_call")
    assert tool_call_id is not None
    await runtime.start_tool(
        tool_execution_id="tool_run_internal",
        tool_call_id=tool_call_id,
        tool_name="read_file",
    )

    checkpointer = MagicMock()
    await handle_step_cancelled(
        cancellation_error=asyncio.CancelledError(),
        state=SimpleNamespace(
            interrupt_request_id="interrupt_internal",
            user_interrupt_reminder_injected=True,
            tool_name="read_file",
        ),
        message_stream_runtime=runtime,
        message_stream_writer=None,
        checkpointer=checkpointer,
        session_id="session",
        thread_id="thread",
        job_id="job_internal",
        turn_id=None,
        execution_id="execution_internal",
        checkpoint_ns="",
        logger=MagicMock(spec=logging.Logger),
    )

    persisted_items = [
        item
        for sink_call in canonical_sink.await_args_list
        for item in sink_call.args[0]
    ]
    tool_call_items = [
        item for item in persisted_items if item.semantic_kind == "tool_call"
    ]
    assert len(tool_call_items) == 1
    assert tool_call_items[0].turn_id is None
    assert runtime._canonical_turn_id is None
    tool_result_sink.assert_awaited_once()
    completion: ToolResultCompletion = tool_result_sink.await_args.args[0]
    assert completion.model_call_id == "model_internal_cancel"
    assert completion.tool_call_id == tool_call_id
    assert completion.tool_invocation_id == "tool-invocation:model_internal_cancel:provider_call"
    assert completion.tool_attempt_id == "tool_run_internal"
    assert completion.tool_name == "read_file"
    assert completion.tool_outcome == "unknown"
    outcome_sink.assert_awaited_with("model_internal_cancel", "cancelled")
    checkpointer.mark_execution_lost.assert_called_once_with(
        "session",
        thread_id="thread",
        turn_id=None,
        execution_id="execution_internal",
        reason="user_interrupt",
        checkpoint_ns="",
    )


@pytest.mark.asyncio
async def test_user_interrupt_with_exception_closes_original_turn_stream() -> None:
    runtime, writer = AsyncMock(), AsyncMock()
    checkpointer, publish = MagicMock(), AsyncMock()
    await handle_step_failure(
        error=RuntimeError("工具取消后抛出异常"),
        interrupt_state=SimpleNamespace(interrupt_request_id="interrupt-original"),
        message_stream_runtime=runtime, message_stream_writer=writer,
        checkpointer=checkpointer, session_id="session", thread_id="thread",
        job_id="job", turn_id="turn", execution_id="execution", checkpoint_ns="",
        publish=publish, logger=MagicMock(spec=logging.Logger),
    )
    runtime.finalize_interruption_facts.assert_awaited_once_with()
    writer.close_interrupted.assert_awaited_once_with("interrupt-original")
    writer.close_failed.assert_not_awaited()
    publish.assert_not_awaited()
