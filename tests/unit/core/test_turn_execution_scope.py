from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    ScopeCancelledError,
    TurnExecutionScope,
)


@pytest.mark.asyncio
async def test_scope_cancellation_cascades_and_runs_hooks() -> None:
    parent = TurnExecutionScope("stream_1")
    child = parent.child("tool_1")
    calls: list[str] = []
    parent.register_cleanup(lambda: calls.append("parent"))
    child.register_cleanup(lambda: calls.append("child"))

    assert await parent.cancel("user_requested") is True
    assert parent.cancellation_signal.is_cancelled is True
    assert child.cancellation_signal.is_cancelled is True
    with pytest.raises(ScopeCancelledError):
        child.cancellation_signal.raise_if_cancelled()

    await parent.close()
    assert calls == ["child", "parent"]


@pytest.mark.asyncio
async def test_local_scope_deadline_does_not_cancel_parent() -> None:
    parent = TurnExecutionScope("stream_1")
    child = parent.child("model_1", deadline=time.monotonic() - 1)

    assert await child.enforce_deadline() is True
    assert child.cancellation_signal.reason == "scope_deadline_exceeded"
    assert parent.cancellation_signal.is_cancelled is False


def test_control_inbox_is_idempotent_and_ordered() -> None:
    inbox = AgentControlInbox("stream_1")
    first = inbox.accept(
        command_id="cmd_1",
        kind="interrupt",
        idempotency_key="idem_1",
        payload={"reason": "user_requested"},
    )
    duplicate = inbox.accept(
        command_id="cmd_2",
        kind="interrupt",
        idempotency_key="idem_1",
    )

    assert duplicate == first
    assert inbox.snapshot() == [first]


def test_control_inbox_persists_intent_but_does_not_auto_replay_after_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "control.json"
    inbox = AgentControlInbox("stream_1", state_path=state_path)
    command = inbox.accept(
        command_id="cmd_1",
        kind="resource.operation.result",
        idempotency_key="idem_1",
    )

    restarted = AgentControlInbox("stream_1", state_path=state_path)
    assert restarted.recoverable() == [command]
    assert restarted._pending.empty()
    restarted.mark("cmd_1", "rejected")
    assert AgentControlInbox("stream_1", state_path=state_path).recoverable() == []


@pytest.mark.asyncio
async def test_control_inbox_consumed_state_is_explicit() -> None:
    inbox = AgentControlInbox("stream_1")
    inbox.accept(
        command_id="cmd_1",
        kind="resume",
        idempotency_key="idem_1",
    )
    command = await asyncio.wait_for(inbox.next(), timeout=1)
    consumed = inbox.mark(command.command_id, "consumed")
    assert consumed.status == "consumed"


@pytest.mark.asyncio
async def test_agent_loop_control_coordinator_rejects_late_non_interrupt_control() -> None:
    scope = TurnExecutionScope("stream_1")
    inbox = AgentControlInbox("stream_1")
    writer = AsyncMock()
    coordinator = AgentLoopControlCoordinator(scope, inbox, writer)
    command = inbox.accept(
        command_id="cmd_steer",
        kind="steer",
        idempotency_key="idem_steer",
    )

    await scope.cancel("user_requested")
    result = await coordinator.process(command)

    assert result["status"] == "rejected"
    assert inbox.get(command.command_id).status == "rejected"
    writer.commit.assert_not_awaited()
    await scope.close()


@pytest.mark.asyncio
async def test_agent_loop_control_coordinator_linearizes_interrupt_once() -> None:
    scope = TurnExecutionScope("stream_1")
    inbox = AgentControlInbox("stream_1")
    writer = AsyncMock()
    writer.commit.return_value = {
        "type": "interrupt.requested",
        "event_seq": 1,
    }
    coordinator = AgentLoopControlCoordinator(scope, inbox, writer)
    command = inbox.accept(
        command_id="cmd_interrupt",
        kind="interrupt",
        idempotency_key="idem_interrupt",
        payload={"reason": "user_requested"},
    )

    result = await coordinator.process(command)

    assert result["type"] == "interrupt.requested"
    assert scope.cancellation_signal.reason == "user_requested"
    assert inbox.get(command.command_id).status == "consumed"
    assert writer.commit.await_count == 1
    await scope.close()
