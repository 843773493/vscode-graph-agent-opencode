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
    TurnControlCoordinator,
    TurnExecutionScope,
    TurnExecutionScopeRegistry,
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


@pytest.mark.asyncio
async def test_closing_child_detaches_parent_cancellation_hook() -> None:
    parent = TurnExecutionScope("stream_1")
    child = parent.child("tool_1")

    await child.close()
    assert await parent.cancel("user_requested") is True
    assert child.cancellation_signal.is_cancelled is False

    await parent.close()


@pytest.mark.asyncio
async def test_parent_close_releases_child_signal_and_deadline() -> None:
    parent = TurnExecutionScope("parent")
    child = parent.child("child", timeout_seconds=60)
    grandchild = child.child("grandchild", timeout_seconds=60)

    await parent.close()
    await parent.cancel("late-parent-cancel")

    assert child.is_closed and grandchild.is_closed
    assert not child.cancellation_signal.is_cancelled
    assert not grandchild.cancellation_signal.is_cancelled
    assert child._deadline_handle is None
    assert grandchild._deadline_handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_kind", ["abort", "signal"])
async def test_late_cancel_hook_is_owned_and_drained_on_close(hook_kind: str) -> None:
    scope = TurnExecutionScope("late-hook")
    started = asyncio.Event()
    calls: list[str] = []
    await scope.cancel("user_requested")

    async def hook(_reason: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("hook-drained")

    if hook_kind == "abort":
        scope.register_abort(hook)
    else:
        scope.cancellation_signal.add_hook(hook)
    scope.register_cleanup(lambda: calls.append("handle-released"))
    await started.wait()
    assert scope.lifetime_scope.snapshot().task_count == 1

    await scope.close()

    assert calls == ["hook-drained", "handle-released"]
    assert scope.lifetime_scope.snapshot().task_count == 0


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
    assert restarted.snapshot() == [command]
    assert restarted._pending.empty()
    restarted.mark("cmd_1", "rejected")
    rejected = AgentControlInbox("stream_1", state_path=state_path).snapshot()
    assert [item.status for item in rejected] == ["rejected"]


@pytest.mark.asyncio
async def test_control_inbox_state_is_reaped_on_turn_close(tmp_path: Path) -> None:
    """Turn 收敛即确定性删除控制状态文件，不随 Turn 数无限堆积。"""
    state_path = tmp_path / "control.json"
    registry = TurnExecutionScopeRegistry()
    registry.create("stream_1")
    inbox = AgentControlInbox("stream_1", session_id="ses_1", state_path=state_path)
    inbox.accept(
        command_id="cmd_1",
        kind="interrupt",
        idempotency_key="idem_1",
    )
    registry.register_inbox("stream_1", inbox)
    assert state_path.is_file()

    await registry.close("stream_1")

    assert not state_path.exists()
    assert registry.get_inbox("stream_1") is None


def test_control_inbox_rejects_state_file_from_another_session(tmp_path: Path) -> None:
    """控制状态文件自述的 session_id 与请求归属不符时 fail closed。"""
    state_path = tmp_path / "control.json"
    AgentControlInbox(
        "stream_1",
        session_id="ses_owner",
        state_path=state_path,
    ).accept(
        command_id="cmd_1",
        kind="interrupt",
        idempotency_key="idem_1",
    )

    with pytest.raises(RuntimeError, match="session_id 不匹配"):
        AgentControlInbox(
            "stream_1",
            session_id="ses_other",
            state_path=state_path,
        )


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


@pytest.mark.asyncio
async def test_interrupt_prepares_owner_before_cancel_and_concurrent_loop_consumption() -> None:
    scope, inbox = TurnExecutionScope("stream_1"), AgentControlInbox("stream_1")
    writer, accepted = AsyncMock(), []
    writing, release = asyncio.Event(), asyncio.Event()

    async def commit(*_args, **_kwargs):
        writing.set()
        await release.wait()
        return {"type": "interrupt.requested", "event_seq": 1}

    async def cancel_hook(_reason):
        assert accepted == ["original-request"]

    writer.commit.side_effect = commit
    scope.register_abort(cancel_hook)
    direct = asyncio.create_task(TurnControlCoordinator(scope, inbox, writer).submit_interrupt(
        command_id="original-request", idempotency_key="original-request",
        on_accepted=lambda: accepted.append("original-request"),
    ))
    await writing.wait()
    command = await inbox.next()
    loop = asyncio.create_task(AgentLoopControlCoordinator(scope, inbox, writer).process(command))
    await asyncio.sleep(0)
    assert not scope.cancellation_signal.is_cancelled
    assert not accepted
    release.set()
    await asyncio.gather(direct, loop)
    assert accepted == ["original-request"]
    assert scope.cancellation_signal.reason == "user_requested"
    assert inbox.get(command.command_id).status == "consumed"
    writer.commit.assert_awaited_once()
    await scope.close()


@pytest.mark.asyncio
async def test_rejected_interrupt_never_prepares_owner_or_cancels() -> None:
    scope, inbox, writer = TurnExecutionScope("stream_1"), AgentControlInbox("stream_1"), AsyncMock()
    accepted = []
    writer.commit.return_value = {"type": "interrupt.rejected"}
    await TurnControlCoordinator(scope, inbox, writer).submit_interrupt(
        command_id="late", idempotency_key="late", on_accepted=lambda: accepted.append("late"),
    )
    assert not accepted
    assert not scope.cancellation_signal.is_cancelled
    assert inbox.get("late").status == "rejected"
    await scope.close()


@pytest.mark.asyncio
async def test_accepted_interrupt_cancels_and_settles_even_if_owner_publication_fails() -> None:
    scope, inbox, writer = TurnExecutionScope("stream_1"), AgentControlInbox("stream_1"), AsyncMock()
    writer.commit.return_value = {"type": "interrupt.requested"}
    failure = RuntimeError("reminder 写入失败")

    def fail_publication():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await TurnControlCoordinator(scope, inbox, writer).submit_interrupt(
            command_id="accepted", idempotency_key="accepted", on_accepted=fail_publication,
        )
    assert caught.value is failure
    assert scope.cancellation_signal.reason == "user_requested"
    assert inbox.get("accepted").status == "consumed"
    queued = await inbox.next()
    assert (await AgentLoopControlCoordinator(scope, inbox, writer).process(queued))["status"] == "consumed"
    writer.commit.assert_awaited_once()
    await scope.close()


@pytest.mark.asyncio
async def test_stale_queued_interrupt_cannot_rewrite_consumed_status() -> None:
    scope, inbox, writer = TurnExecutionScope("stream_1"), AgentControlInbox("stream_1"), AsyncMock()
    writer.commit.return_value = {"type": "interrupt.requested"}
    await TurnControlCoordinator(scope, inbox, writer).submit_interrupt(command_id="cmd", idempotency_key="cmd")
    queued = await inbox.next()
    result = await AgentLoopControlCoordinator(scope, inbox, writer).process(queued)
    assert result["status"] == "consumed"
    assert inbox.get("cmd").status == "consumed"
    writer.commit.assert_awaited_once()
    with pytest.raises(RuntimeError, match="终态不可改写"):
        inbox.mark("cmd", "rejected")
    await scope.close()
