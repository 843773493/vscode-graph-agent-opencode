from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    TurnControlCoordinator,
    TurnExecutionScope,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.orchestration.activity_runtime import (
    ActivityCapabilities,
    ActivityHandlerRegistry,
    ActivityRuntime,
    DefaultActivityHandler,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


@pytest.fixture
def runtime_context() -> tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path]:
    output_root = (
        Path.cwd()
        / "out/tests/integration/backend/agents/test_turn_message_stream_runtime"
    )
    workspace_root = output_root / "workspace"
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    resource_state_path = output_root / "runtime" / "resources.json"
    resource_state_path.unlink(missing_ok=True)
    resource_state_path.with_suffix(".tmp").unlink(missing_ok=True)
    sessions_root = workspace_root / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    # canonical session_id 必须是 UUIDv4 位 profile（第 13 位为 4、第 17 位属 8/9/a/b）
    session_id = "ses_019bc29b34e276e6870ef6d46174d4da"
    seed_catalog_session_bundle(sessions_root, session_id)
    return (
        MessageStreamStore(path_resolver=resolver),
        resolver,
        session_id,
        resource_state_path,
    )


@pytest.mark.asyncio
async def test_model_tool_scopes_share_turn_cancellation_and_close_provider(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    store, _, session_id, _ = runtime_context
    writer = await store.open(thread_id=store._path_resolver.main_thread_id(session_id), session_id=session_id, turn_id="job_scope_propagation")
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
async def test_concurrent_turn_events_allocate_contiguous_event_sequences(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    store, _, session_id, _ = runtime_context
    writer = await store.open(thread_id=store._path_resolver.main_thread_id(session_id), session_id=session_id, turn_id="job_concurrent_events")

    await asyncio.gather(
        *(
            writer.commit(
                "block.delta",
                {
                    "block_id": f"block_{index}",
                    "block_index": index,
                    "carrier_type": "text",
                    "operation": "append",
                    "text": str(index),
                },
                block_id=f"block_{index}",
            )
            for index in range(12)
        )
    )

    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    assert [event["event_seq"] for event in events] == list(range(1, 14))


@pytest.mark.asyncio
async def test_model_call_local_deadline_does_not_cancel_turn(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
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
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    _, _, _, resource_state_path = runtime_context
    manager = ExternalResourceLeaseLedger(state_path=resource_state_path)
    manager.register_external(
        resource_id="browser_1",
        kind="browser_context",
        lifetime_scope="session",
    )
    lease = manager.acquire_operation(
        resource_id="browser_1",
        turn_stream_id="stream_resource",
        operation_id="navigate_1",
    )

    released = manager.release_turn_leases("stream_resource")
    assert [item.lease_id for item in released] == [lease.lease_id]
    assert manager.get("browser_1").status == "running"  # type: ignore[union-attr]

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
    restarted = ExternalResourceLeaseLedger(state_path=resource_state_path)
    records = await restarted.reconcile({"browser_1": "stopped", "mcp_1": "running"})
    statuses = {record.resource_id: record.status for record in records}
    assert statuses["mcp_1"] == "recovered"
    assert restarted.leases_for_turn("stream_crashed")[0].status == "reconcile_required"


@pytest.mark.asyncio
async def test_control_race_accepts_interrupt_once_and_rejects_steer(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    store, _, session_id, _ = runtime_context
    writer = await store.open(thread_id=store._path_resolver.main_thread_id(session_id), session_id=session_id, turn_id="job_control_race")
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
async def test_failed_control_acceptance_retries_once_before_interrupting_real_stream(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, session_id, resource_state_path = runtime_context
    writer = await store.open(
        thread_id=store._path_resolver.main_thread_id(session_id),
        session_id=session_id, turn_id="job_control_write_retry",
    )
    scope = TurnExecutionScope(writer.turn_stream_id)
    state_path = resource_state_path.with_name("control.json")
    state_path.unlink(missing_ok=True)
    inbox = AgentControlInbox(writer.turn_stream_id, session_id=session_id, state_path=state_path)
    coordinator = TurnControlCoordinator(scope, inbox, writer)
    cancellations: list[str] = []
    scope.register_abort(cancellations.append)
    initial = await store.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id)
    failure = OSError("控制日志原子替换失败")
    replace = Path.replace

    def fail_control_replace(source: Path, target: str | Path) -> Path:
        if source == state_path.with_suffix(".tmp"):
            raise failure
        return replace(source, target)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_control_replace)
            with pytest.raises(OSError) as caught:
                await coordinator.submit_interrupt(command_id="cmd_retry", idempotency_key="idem_retry")
        assert caught.value is failure
        assert not scope.cancellation_signal.is_cancelled
        assert cancellations == []
        assert inbox.snapshot() == []
        assert inbox._pending.empty()
        assert not state_path.exists()
        assert await store.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id) == initial

        accepted, duplicate = await asyncio.gather(
            coordinator.submit_interrupt(command_id="cmd_retry", idempotency_key="idem_retry"),
            coordinator.submit_interrupt(command_id="cmd_duplicate", idempotency_key="idem_retry"),
        )
        assert accepted["type"] == "interrupt.requested"
        assert accepted["command_id"] == "cmd_retry"
        assert duplicate == {"status": "consumed", "command_id": "cmd_retry"}
        assert cancellations == ["user_requested"]
        assert scope.cancellation_signal.reason == "user_requested"
        queued = await inbox.next()
        assert queued.command_id == "cmd_retry"
        assert queued.control_seq == 1
        assert inbox._pending.empty()
        assert await AgentLoopControlCoordinator(scope, inbox, writer).process(queued) == duplicate
        restored = AgentControlInbox(writer.turn_stream_id, session_id=session_id, state_path=state_path)
        assert restored.snapshot() == inbox.snapshot()
        assert [command.status for command in restored.snapshot()] == ["consumed"]
        assert restored._pending.empty()

        await writer.close_interrupted("cmd_retry")
        restarted = MessageStreamStore(path_resolver=store._path_resolver)
        await restarted.open_existing(
            session_id=session_id, thread_id=writer.thread_id,
            turn_id="job_control_write_retry", turn_stream_id=writer.turn_stream_id,
        )
        events = await restarted.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id)
        interrupts = [event for event in events if event["type"] == "interrupt.requested"]
        assert len(interrupts) == 1
        assert interrupts[0]["event_id"] == "cmd_retry"
        assert interrupts[0]["payload"]["interrupt_request_id"] == "cmd_retry"
        assert (await restarted.get_state(writer.turn_stream_id))["stream_status"] == "interrupted"
        assert cancellations == ["user_requested"]
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_background_turn_and_snapshot_recovers(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    store, resolver, session_id, _ = runtime_context
    writer = await store.open(thread_id=store._path_resolver.main_thread_id(session_id), session_id=session_id, turn_id="job_disconnect")
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
        thread_id=restarted._path_resolver.main_thread_id(session_id), session_id=session_id,
        turn_id="job_disconnect",
        turn_stream_id=writer.turn_stream_id,
    )
    recovered_snapshot = await recovered_writer.snapshot()
    assert recovered_snapshot["payload"]["blocks"][0]["text"] == "后台继续"
    await scope.close()


@pytest.mark.asyncio
async def test_crash_after_interrupt_request_recovers_execution_lost(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
) -> None:
    store, resolver, session_id, _ = runtime_context
    writer = await store.open(thread_id=store._path_resolver.main_thread_id(session_id), session_id=session_id, turn_id="job_crash_recovery")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("native_path", ["/private-activity/detail.json", r"C:\private-activity\detail.json", r"\\server\private-activity\detail.json"])
@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
async def test_activity_projection_failure_keeps_native_location_in_diagnostics(
    runtime_context: tuple[MessageStreamStore, SessionCatalogPathResolver, str, Path],
    native_path: str, error_type: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    store, _, session_id, _ = runtime_context
    error = error_type(13, f"投影无法读取 {native_path}", native_path) if error_type is PermissionError else error_type(native_path)

    class RepairableHandler(DefaultActivityHandler):
        failure: Exception | None = error

        def normalize(self, payload: Mapping[str, object]) -> Mapping[str, object]:
            if self.failure is not None:
                raise self.failure
            return super().normalize(payload)

    handler = RepairableHandler()
    registry = ActivityHandlerRegistry()
    registry.register(kind="test.projection", handler=handler, capabilities=ActivityCapabilities(detail=True))
    writer = await store.open(
        session_id=session_id, thread_id=store._path_resolver.main_thread_id(session_id), turn_id="activity-error",
    )
    runtime = ActivityRuntime(writer, registry)
    subscription = await store.subscribe(writer.turn_stream_id)
    events = [
        await runtime.started(activity_id="activity-1", kind="test.projection", summary="处理资源"),
        await runtime.updated(activity_id="activity-1", kind="test.projection", summary="读取详情"),
        await runtime.completed(activity_id="activity-1", kind="test.projection", summary="处理完成"),
    ]
    for event in events:
        payload = event["payload"]
        assert isinstance(payload, dict)
        assert payload["detail_available"] is False
        detail_error = payload["detail_error"]
        assert isinstance(detail_error, str)
        assert "phase=projection" in detail_error
        assert error_type.__name__ in detail_error
        assert "private-activity" not in json.dumps(event)
        if error_type is PermissionError:
            assert "errno=13" in detail_error
        assert (await subscription.get()).event == event
    await store.unsubscribe(subscription)
    assert len([record for record in caplog.records if record.exc_info and record.exc_info[1] is error]) == 3

    # 同一owner修复后正常发布，不替换handler或消息流。
    handler.failure = None
    repaired = await runtime.started(activity_id="activity-2", kind="test.projection", summary="再次处理")
    assert isinstance(repaired["payload"], dict)
    assert "detail_error" not in repaired["payload"]
    await runtime.completed(activity_id="activity-2", kind="test.projection", summary="再次处理完成")
    await writer.close_completed()
    replay = await store.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id)
    assert [event["event_seq"] for event in replay] == list(range(1, 8))
    snapshot = await writer.snapshot()
    assert [activity["status"] for activity in snapshot["payload"]["activities"]] == ["completed", "completed"]

    # 检查实际JSONL与state文件，不能只证明返回值经过了安全投影。
    for path in (store._stream_path(session_id, writer.turn_stream_id), store._state_path(session_id, writer.turn_stream_id)):
        assert "private-activity" not in path.read_text()
    restarted = MessageStreamStore(path_resolver=store._path_resolver)
    cold = await restarted.open_existing(
        session_id=session_id, thread_id=writer.thread_id, turn_id="activity-error", turn_stream_id=writer.turn_stream_id,
    )
    assert await restarted.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id) == replay
    recovered = await cold.snapshot()
    assert recovered["payload"] == snapshot["payload"]
    assert "private-activity" not in json.dumps(recovered)
