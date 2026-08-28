from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_paths import SessionPathResolver
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    TurnExecutionScope,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.resource_manager import ResourceManager
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime


@pytest.fixture
def runtime_context() -> tuple[MessageStreamStore, SessionPathResolver, str, Path]:
    output_root = (
        Path.cwd()
        / "out/tests/integration/backend/agents/test_turn_message_stream_runtime"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    sessions_root = output_root / "workspace" / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    session_id = "ses_turn_runtime_integration"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=session_id,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        f'{{"session_id":"{session_id}","title":"{session_id}",'
        f'"created_at":"{now}","updated_at":"{now}"}}',
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return (
        MessageStreamStore(path_resolver=resolver),
        resolver,
        session_id,
        output_root,
    )


@pytest.mark.asyncio
async def test_model_tool_scopes_share_turn_cancellation_and_close_provider(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    store, _, session_id, _ = runtime_context
    writer = await store.open(session_id=session_id, turn_id="job_scope_propagation")
    runtime = MessageStreamRuntime(writer)
    turn_scope = TurnExecutionScope(writer.turn_stream_id)
    model_scope = turn_scope.child("model-1")
    tool_scope = turn_scope.child("tool-1")
    closed: list[str] = []

    async def close_provider(_reason: str) -> None:
        closed.append("provider")

    model_scope.register_abort(close_provider)
    tool_scope.register_abort(lambda _reason: closed.append("tool"))

    await runtime.start_model("model_1", "primary")
    await writer.commit(
        "block.started",
        {"block_id": "text_1", "block_index": 0, "carrier_type": "text"},
        block_id="text_1",
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "text_1",
            "block_index": 0,
            "carrier_type": "text",
            "operation": "append",
            "text": "已提交",
        },
        block_id="text_1",
    )
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_scope", "reason": "user_requested"},
    )
    assert await turn_scope.cancel("user_requested") is True
    await runtime.finalize_interruption_facts()
    await writer.close_interrupted("intr_scope")

    assert model_scope.cancellation_signal.is_cancelled is True
    assert tool_scope.cancellation_signal.is_cancelled is True
    assert sorted(closed) == ["provider", "tool"]
    state = await store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "interrupted"
    assert state["blocks"][0]["partial"] is True
    await turn_scope.close()


@pytest.mark.asyncio
async def test_model_call_local_deadline_does_not_cancel_turn(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    turn_scope = TurnExecutionScope("stream_timeout")
    model_scope = turn_scope.child("model-1", timeout_seconds=0.001)
    await asyncio.sleep(0.01)

    assert await model_scope.enforce_deadline() is True
    assert model_scope.cancellation_signal.reason == "scope_deadline_exceeded"
    assert turn_scope.cancellation_signal.is_cancelled is False
    await turn_scope.close()


@pytest.mark.asyncio
async def test_resource_cancel_stop_and_crash_reconcile_are_independent(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    _, _, _, output_root = runtime_context
    stopped: list[str] = []
    manager = ResourceManager(state_path=output_root / "resources.json")
    manager.register_external(
        resource_id="browser_1",
        kind="browser_context",
        lifetime_scope="session",
        stopper=lambda: stopped.append("browser_1"),
    )
    lease = manager.acquire_operation(
        resource_id="browser_1",
        turn_stream_id="stream_resource",
        operation_id="navigate_1",
    )

    released = await manager.cancel_turn("stream_resource")
    assert [item.lease_id for item in released] == [lease.lease_id]
    assert manager.get("browser_1").status == "running"  # type: ignore[union-attr]
    assert stopped == []

    await manager.stop(resource_id="browser_1")
    assert manager.get("browser_1").status == "stopped"  # type: ignore[union-attr]
    assert stopped == ["browser_1"]

    manager.register_external(
        resource_id="mcp_1",
        kind="mcp_connection",
        lifetime_scope="workspace",
    )
    manager.acquire_operation(
        resource_id="mcp_1",
        turn_stream_id="stream_crashed",
        operation_id="call_1",
    )
    restarted = ResourceManager(state_path=output_root / "resources.json")
    records = restarted.reconcile({"browser_1": "stopped", "mcp_1": "running"})
    statuses = {record.resource_id: record.status for record in records}
    assert statuses["mcp_1"] == "recovered"
    assert restarted.leases_for_turn("stream_crashed")[0].status == "reconcile_required"


@pytest.mark.asyncio
async def test_control_race_accepts_interrupt_once_and_rejects_steer(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    store, _, session_id, _ = runtime_context
    writer = await store.open(session_id=session_id, turn_id="job_control_race")
    scope = TurnExecutionScope(writer.turn_stream_id)
    inbox = AgentControlInbox(writer.turn_stream_id)
    coordinator = AgentLoopControlCoordinator(scope, inbox, writer)
    interrupt = inbox.accept(
        command_id="cmd_interrupt",
        kind="interrupt",
        idempotency_key="idem_interrupt",
        payload={"reason": "user_requested"},
    )
    steer = inbox.accept(
        command_id="cmd_steer",
        kind="steer",
        idempotency_key="idem_steer",
    )

    results = await asyncio.gather(
        coordinator.process(interrupt),
        coordinator.process(steer),
    )
    assert results[0]["type"] == "interrupt.requested"
    assert results[1]["status"] == "rejected"
    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    assert [event["type"] for event in events].count("interrupt.requested") == 1
    await scope.close()


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_background_turn_and_snapshot_recovers(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    store, resolver, session_id, _ = runtime_context
    writer = await store.open(session_id=session_id, turn_id="job_disconnect")
    scope = TurnExecutionScope(writer.turn_stream_id)
    subscription = await store.subscribe(writer.turn_stream_id)
    await store.unsubscribe(subscription)

    await writer.commit(
        "block.delta",
        {
            "block_id": "block_background",
            "block_index": 0,
            "carrier_type": "text",
            "operation": "append",
            "text": "后台继续",
        },
        block_id="block_background",
    )
    assert scope.cancellation_signal.is_cancelled is False
    snapshot = await writer.snapshot()
    assert snapshot["payload"]["blocks"][0]["text"] == "后台继续"
    restarted = MessageStreamStore(path_resolver=resolver)
    recovered_writer = await restarted.open_existing(
        session_id=session_id,
        turn_id="job_disconnect",
        turn_stream_id=writer.turn_stream_id,
    )
    recovered_snapshot = await recovered_writer.snapshot()
    assert recovered_snapshot["payload"]["blocks"][0]["text"] == "后台继续"
    await scope.close()


@pytest.mark.asyncio
async def test_crash_after_interrupt_request_recovers_execution_lost(
    runtime_context: tuple[MessageStreamStore, SessionPathResolver, str, Path],
) -> None:
    store, resolver, session_id, _ = runtime_context
    writer = await store.open(session_id=session_id, turn_id="job_crash_recovery")
    await writer.commit(
        "tool.started",
        {
            "tool_execution_id": "tool_crash",
            "tool_call_id": "call_crash",
            "tool_name": "write_file",
        },
        tool_execution_id="tool_crash",
    )
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_crash", "reason": "user_requested"},
    )

    restarted = MessageStreamStore(path_resolver=resolver)
    assert await restarted.reconcile_unfinished_streams() == 1
    state = await restarted.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "failed"
    assert state["failure"]["code"] == "execution_lost"
    assert state["failure"]["after_interrupt_requested"] is True
    assert state["tool_executions"][0]["outcome"] == "outcome_unknown"
