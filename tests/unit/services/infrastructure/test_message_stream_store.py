from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.services.infrastructure.message_stream_store as message_stream_store_module
from app.core.session_paths import SessionPathResolver
from app.services.infrastructure.message_stream_store import (
    MessageStreamCursorGoneError,
    MessageStreamError,
    MessageStreamStore,
    MessageStreamTerminalError,
)
from app.services.orchestration.activity_runtime import (
    ActivityHandlerRegistry,
    ActivityRuntime,
)


@pytest.fixture
def message_stream_store() -> tuple[MessageStreamStore, SessionPathResolver, str]:
    output_root = (
        Path.cwd()
        / "out/tests/unit/services/infrastructure/test_message_stream_store"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    sessions_root = output_root / "workspace" / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    session_id = "ses_message_stream_test"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=session_id,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": session_id,
                "created_at": now,
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return MessageStreamStore(path_resolver=resolver), resolver, session_id


@pytest.mark.asyncio
async def test_event_commit_is_idempotent_and_terminal_gate_is_strict(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_message_stream_test")
    subscription = await store.subscribe(writer.turn_stream_id)
    first = await writer.commit(
        "block.started",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "reasoning",
        },
        block_id="block_1",
        event_id="evt_idempotent",
    )
    duplicate = await writer.commit(
        "block.started",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "reasoning",
        },
        block_id="block_1",
        event_id="evt_idempotent",
    )
    assert duplicate == first
    assert (await subscription.get()).event["event_id"] == "evt_idempotent"

    await writer.commit(
        "block.completed",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "reasoning",
            "status": "completed",
            "completion_reason": "upstream_completed",
        },
        block_id="block_1",
    )
    await writer.close_completed()
    with pytest.raises(MessageStreamTerminalError):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "carrier_type": "reasoning",
                "operation": "append",
                "text": "迟到",
            },
            block_id="block_1",
        )
    rejected = await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_late", "reason": "user_requested"},
    )
    assert rejected["type"] == "interrupt.rejected"

    await store.unsubscribe(subscription)
    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    assert "stream.completed" in [event["type"] for event in events]
    assert events[-1]["type"] == "interrupt.rejected"


@pytest.mark.asyncio
async def test_stream_terminal_event_survives_unrelated_catalog_drift(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    session_dir = resolver.resolve_session_node(session_id)
    (session_dir.parent / "ses_unindexed_runtime_drift_12345678").mkdir()

    writer = await store.open(
        session_id=session_id,
        turn_id="job_runtime_drift_terminal",
    )
    await writer.close_failed(
        code="execution_error",
        message="工具执行失败",
        resumable=True,
    )

    state = await store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "failed"
    assert state["failure"]["code"] == "execution_error"


@pytest.mark.asyncio
async def test_stream_events_do_not_duplicate_full_checkpoint_on_disk(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_compact_checkpoint")
    for _ in range(32):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "carrier_type": "text",
                "operation": "append",
                "text": "x" * 256,
            },
            block_id="block_1",
        )

    stream_path = (
        resolver.resolve_session_node(session_id)
        / "message_streams"
        / f"{writer.turn_stream_id}.jsonl"
    )
    state_path = stream_path.with_suffix(".state.json")
    records = [json.loads(line) for line in stream_path.read_text().splitlines()]
    assert all(record["checkpoint"] == {} for record in records)
    assert state_path.is_file()
    assert stream_path.stat().st_size < 100_000

    restarted = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted.open(
        session_id=session_id,
        turn_id="job_compact_checkpoint",
    )
    state = await restarted.get_state(restarted_writer.turn_stream_id)
    assert state["snapshot_seq"] == 33
    assert state["blocks"][0]["text"] == "x" * (256 * 32)


@pytest.mark.asyncio
async def test_oversized_stream_retains_tail_and_recovers_from_snapshot(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resolver, session_id = message_stream_store
    monkeypatch.setattr(message_stream_store_module, "MESSAGE_STREAM_MAX_BYTES", 1_000)
    monkeypatch.setattr(message_stream_store_module, "MESSAGE_STREAM_RETAINED_BYTES", 260)
    writer = await store.open(session_id=session_id, turn_id="job_stream_retention")

    for index in range(12):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "operation": "append",
                "text": f"event-{index}",
            },
            block_id="block_1",
        )

    stream_path = (
        resolver.resolve_session_node(session_id)
        / "message_streams"
        / f"{writer.turn_stream_id}.jsonl"
    )
    assert stream_path.stat().st_size < 1_000
    with pytest.raises(MessageStreamCursorGoneError):
        await store.list_events(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=0,
        )

    snapshot = await store.snapshot_event(writer.turn_stream_id)
    assert snapshot["payload"]["snapshot_seq"] == 13
    assert snapshot["payload"]["blocks"][0]["text"].endswith("event-11")

    restarted = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted.open(
        session_id=session_id,
        turn_id="job_stream_retention",
    )
    restarted_state = await restarted.get_state(restarted_writer.turn_stream_id)
    assert restarted_state["snapshot_seq"] == 13
    assert restarted_state["blocks"][0]["text"].endswith("event-11")


@pytest.mark.asyncio
async def test_completed_stream_rejects_unfinished_entities(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store

    model_writer = await store.open(
        session_id=session_id,
        turn_id="job_completed_with_model_running",
    )
    await model_writer.commit(
        "model.started",
        {"model_call_id": "model_1", "attempt": 1, "model": "test"},
        model_call_id="model_1",
    )
    with pytest.raises(MessageStreamError, match="model_calls"):
        await model_writer.close_completed()
    assert (await store.get_state(model_writer.turn_stream_id))["stream_status"] == "open"

    tool_writer = await store.open(
        session_id=session_id,
        turn_id="job_completed_with_tool_running",
    )
    await tool_writer.commit(
        "tool.started",
        {
            "tool_execution_id": "exec_1",
            "tool_call_id": "call_1",
            "tool_name": "shell",
        },
        tool_execution_id="exec_1",
    )
    with pytest.raises(MessageStreamError, match="tool_executions"):
        await tool_writer.close_completed()

    activity_writer = await store.open(
        session_id=session_id,
        turn_id="job_completed_with_activity_running",
    )
    await activity_writer.commit(
        "activity.started",
        {
            "activity_id": "activity_1",
            "kind": "context.compaction",
            "status": "running",
        },
    )
    with pytest.raises(MessageStreamError, match="activities"):
        await activity_writer.close_completed()


@pytest.mark.asyncio
async def test_existing_stream_ids_skip_legacy_turns_without_creating_streams(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_with_stream")

    streams = await store.existing_stream_ids(
        session_id=session_id,
        turn_ids=["job_with_stream", "job_legacy"],
    )

    assert streams == {"job_with_stream": writer.turn_stream_id}
    assert not (
        resolver.resolve_session_node(session_id)
        / "message_streams"
        / "job_legacy.jsonl"
    ).exists()


@pytest.mark.asyncio
async def test_tool_call_projection_does_not_erase_known_name_or_arguments(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_tool_call_merge")

    await writer.commit(
        "tool_call",
        {
            "tool_call_id": "call_1",
            "tool_name": "invoke_custom_tool",
            "arguments": {"tool_name": "unknown_tool"},
            "status": "streaming",
        },
    )
    await writer.commit(
        "tool_call",
        {
            "tool_call_id": "call_1",
            "tool_name": "",
            "arguments": {},
            "status": "streaming",
        },
    )

    state = await store.get_state(writer.turn_stream_id)
    assert len(state["tool_calls"]) == 1
    tool_call = state["tool_calls"][0]
    assert tool_call["tool_call_id"] == "call_1"
    assert tool_call["tool_name"] == "invoke_custom_tool"
    assert tool_call["arguments"] == {"tool_name": "unknown_tool"}
    assert tool_call["status"] == "streaming"
    assert tool_call["started_seq"] == 2
    assert tool_call["last_event_seq"] == 3
    assert isinstance(tool_call["started_at"], str)
    assert tool_call["updated_at"] == tool_call["started_at"] or isinstance(
        tool_call["updated_at"], str
    )


@pytest.mark.asyncio
async def test_validation_retry_marks_previous_model_blocks_intermediate(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_retry_projection")
    await writer.commit(
        "model.started",
        {"model_call_id": "model_1", "attempt": 1, "model": "primary"},
        model_call_id="model_1",
    )
    await writer.commit(
        "block.started",
        {
            "block_id": "answer_1",
            "block_index": 0,
            "carrier_type": "text",
        },
        model_call_id="model_1",
        block_id="answer_1",
    )
    await writer.commit(
        "block.completed",
        {
            "block_id": "answer_1",
            "block_index": 0,
            "carrier_type": "text",
            "status": "completed",
        },
        model_call_id="model_1",
        block_id="answer_1",
    )
    await writer.commit(
        "model.completed",
        {
            "model_call_id": "model_1",
            "attempt": 1,
            "outcome": "validation_failed",
        },
        model_call_id="model_1",
    )
    await writer.commit(
        "model.retrying",
        {
            "model_call_id": "model_1",
            "attempt": 1,
            "reason": "需要重新请求",
        },
        model_call_id="model_1",
    )

    state = await store.get_state(writer.turn_stream_id)
    assert state["blocks"][0]["model_call_id"] == "model_1"
    assert state["blocks"][0]["projection"] == "intermediate"


@pytest.mark.asyncio
async def test_restart_reconciles_interrupt_and_unknown_tool_result(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_restart_test")
    await writer.commit(
        "tool.started",
        {
            "tool_execution_id": "tool_1",
            "tool_call_id": "call_1",
            "tool_name": "shell",
        },
        tool_execution_id="tool_1",
    )
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_1", "reason": "user_requested"},
    )

    restarted_store = MessageStreamStore(path_resolver=resolver)
    assert await restarted_store.reconcile_unfinished_streams() == 1
    state = await restarted_store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "failed"
    assert state["failure"]["code"] == "execution_lost"
    assert state["failure"]["after_interrupt_requested"] is True
    assert state["tool_executions"][0]["status"] == "completed"
    assert state["tool_executions"][0]["outcome"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_terminal_stream_conservatively_closes_running_activities(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(
        session_id=session_id,
        turn_id="job_activity_terminal_projection",
    )
    await writer.commit(
        "activity.started",
        {
            "activity_id": "approval_1",
            "kind": "approval.wait",
            "status": "running",
            "side_effect_policy": "none",
        },
    )
    await writer.commit(
        "activity.started",
        {
            "activity_id": "resource_1",
            "kind": "resource.operation",
            "status": "running",
            "side_effect_policy": "external",
        },
    )
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_activity", "reason": "user_requested"},
    )
    await writer.close_interrupted("intr_activity")

    state = await store.get_state(writer.turn_stream_id)
    activities = {item["activity_id"]: item for item in state["activities"]}
    assert activities["approval_1"]["status"] == "completed"
    assert activities["approval_1"]["outcome"] == "user_interrupt"
    assert activities["resource_1"]["status"] == "failed"
    assert activities["resource_1"]["outcome"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_interrupt_gate_rejects_late_delta_and_preserves_request(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_interrupt_gate")
    await writer.commit(
        "block.started",
        {"block_id": "block_1", "block_index": 0, "carrier_type": "reasoning"},
    )
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_1", "reason": "user_requested"},
    )

    with pytest.raises(MessageStreamTerminalError):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "operation": "append",
                "text": "迟到的 delta",
            },
        )
    with pytest.raises(MessageStreamTerminalError):
        await writer.close_completed()

    repeated = await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_2", "reason": "user_requested"},
        event_id="evt_repeated_interrupt",
    )
    assert repeated["type"] == "interrupt.rejected"
    assert await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_2", "reason": "user_requested"},
        event_id="evt_repeated_interrupt",
    ) == repeated
    state = await store.get_state(writer.turn_stream_id)
    assert state["interrupt_state"] == {
        "request_id": "intr_1",
        "status": "requested",
        "reason": "user_requested",
    }

    await writer.close_interrupted("intr_1")
    state = await store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "interrupted"
    assert state["blocks"][0]["status"] == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage",),
    [("reasoning",), ("text",), ("tool_call",), ("tool_execution",)],
)
async def test_delta_boundary_interrupt_matrix_has_same_live_checkpoint_snapshot_terminal(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
    stage: str,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(
        session_id=session_id,
        turn_id=f"job_delta_interrupt_{stage}",
    )
    subscription = await store.subscribe(writer.turn_stream_id)
    if stage in {"reasoning", "text"}:
        carrier = stage
        await writer.commit(
            "block.started",
            {
                "block_id": f"block_{stage}",
                "block_index": 0,
                "carrier_type": carrier,
            },
            block_id=f"block_{stage}",
        )
        await writer.commit(
            "block.delta",
            {
                "block_id": f"block_{stage}",
                "block_index": 0,
                "carrier_type": carrier,
                "operation": "append",
                "text": f"partial-{stage}",
            },
            block_id=f"block_{stage}",
        )
    elif stage == "tool_call":
        await writer.commit(
            "tool_call.delta",
            {
                "tool_call_id": "call_partial",
                "tool_name": "shell",
                "arguments": {"command": "echo"},
                "status": "accumulating",
                "arguments_complete": False,
            },
        )
    else:
        await writer.commit(
            "tool.started",
            {
                "tool_execution_id": "exec_unknown",
                "tool_call_id": "call_unknown",
                "tool_name": "shell",
            },
            tool_execution_id="exec_unknown",
        )

    await writer.commit(
        "interrupt.requested",
        {
            "interrupt_request_id": f"intr_{stage}",
            "reason": "user_requested",
        },
        event_id=f"intr_{stage}",
    )
    await writer.close_interrupted(f"intr_{stage}")
    live_events = [
        (await subscription.get()).event
        for _ in range(4 if stage in {"reasoning", "text"} else 3)
    ]
    live_types = [event["type"] for event in live_events]
    assert "interrupt.requested" in live_types
    assert live_types[-1] == "stream.interrupted"

    state = await store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "interrupted"
    assert state["snapshot_seq"] == live_events[-1]["event_seq"]
    if stage == "tool_execution":
        assert state["tool_executions"][0]["outcome"] == "outcome_unknown"
    if stage == "tool_call":
        assert state["tool_calls"][0]["status"] in {"cancelled", "incomplete"}
    if stage in {"reasoning", "text"}:
        assert state["blocks"][0]["partial"] is True

    restarted = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted.open_existing(
        session_id=session_id,
        turn_id=f"job_delta_interrupt_{stage}",
        turn_stream_id=writer.turn_stream_id,
    )
    snapshot = await restarted_writer.snapshot()
    assert snapshot["event_seq"] == live_events[-1]["event_seq"]
    assert snapshot["payload"]["stream_status"] == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_and_model_completion_race_has_one_terminal_winner(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_interrupt_completion_race")

    async def request_interrupt() -> dict[str, object]:
        return await writer.commit(
            "interrupt.requested",
            {
                "interrupt_request_id": "intr_race",
                "reason": "user_requested",
            },
            event_id="intr_race",
        )

    results = await asyncio.gather(
        request_interrupt(),
        writer.close_completed(),
        return_exceptions=True,
    )
    state = await store.get_state(writer.turn_stream_id)
    if state["stream_status"] == "interrupting":
        await writer.close_interrupted("intr_race")
        state = await store.get_state(writer.turn_stream_id)

    assert state["stream_status"] in {"completed", "interrupted"}
    types = [
        result["type"]
        for result in results
        if isinstance(result, dict) and isinstance(result.get("type"), str)
    ]
    assert "stream.completed" in types or "interrupt.requested" in types


@pytest.mark.asyncio
async def test_restart_keeps_event_idempotence_and_open_is_serialized(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writers = await asyncio.gather(*(
        store.open(session_id=session_id, turn_id="job_open_race")
        for _ in range(4)
    ))
    assert len({writer.turn_stream_id for writer in writers}) == 1

    first = await writers[0].commit(
        "block.started",
        {"block_id": "block_restart", "carrier_type": "text"},
        event_id="evt_restart_idempotent",
    )
    restarted_store = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted_store.open(
        session_id=session_id,
        turn_id="job_open_race",
    )
    duplicate = await restarted_writer.commit(
        "block.started",
        {"block_id": "block_restart", "carrier_type": "text"},
        event_id="evt_restart_idempotent",
    )
    assert duplicate == first


@pytest.mark.asyncio
async def test_commit_failure_before_persist_does_not_publish_or_recover_delta(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_fsync_failure")

    def fail_encode(*_: object, **__: object) -> str:
        raise OSError("模拟 event/checkpoint 编码失败")

    monkeypatch.setattr(
        "app.services.infrastructure.message_stream_store.json.dumps",
        fail_encode,
    )
    with pytest.raises(OSError, match="编码"):
        await writer.commit(
            "block.delta",
            {"block_id": "block_1", "operation": "append", "text": "不会发布"},
        )

    state = await store.get_state(writer.turn_stream_id)
    assert state["snapshot_seq"] == 1
    assert state["blocks"] == []
    restarted_store = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted_store.open(
        session_id=session_id,
        turn_id="job_fsync_failure",
    )
    restarted_state = await restarted_store.get_state(restarted_writer.turn_stream_id)
    assert restarted_state["snapshot_seq"] == 1
    assert restarted_state["blocks"] == []


@pytest.mark.asyncio
async def test_event_application_failure_does_not_consume_event_seq(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_apply_failure")

    with pytest.raises(MessageStreamError, match="block 事件缺少 block_id"):
        await writer.commit(
            "block.started",
            {"block_index": 0, "carrier_type": "text"},
        )

    event = await writer.commit(
        "block.started",
        {"block_id": "block_1", "block_index": 0, "carrier_type": "text"},
    )
    assert event["event_seq"] == 2


@pytest.mark.asyncio
async def test_fsync_failure_reloads_uncertain_append_before_next_commit(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_fsync_uncertain")
    subscription = await store.subscribe(writer.turn_stream_id)

    original_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("模拟 fsync 边界失败")
        original_fsync(fd)

    monkeypatch.setattr(
        "app.services.infrastructure.message_stream_store.os.fsync",
        fail_once,
    )
    with pytest.raises(OSError, match="fsync"):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "operation": "append",
                "text": "边界事件",
            },
        )

    next_event = await writer.commit(
        "block.delta",
        {
            "block_id": "block_1",
            "operation": "append",
            "text": "后续事件",
        },
    )
    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    seqs = [int(event["event_seq"]) for event in events]
    assert seqs == sorted(set(seqs))
    assert next_event["event_seq"] == seqs[-1]

    # 失败提交没有 live fanout；订阅者只能看到后续成功提交的事件。
    assert subscription.queue.get_nowait().event["event_seq"] == next_event["event_seq"]
    with pytest.raises(asyncio.QueueEmpty):
        subscription.queue.get_nowait()

    restarted_store = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted_store.open(
        session_id=session_id,
        turn_id="job_fsync_uncertain",
    )
    restarted_state = await restarted_store.get_state(restarted_writer.turn_stream_id)
    assert restarted_state["snapshot_seq"] == seqs[-1]
    assert restarted_state["blocks"][0]["text"] in {"边界事件后续事件", "后续事件"}


@pytest.mark.asyncio
async def test_fanout_failure_keeps_durable_event(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_fanout_failure")
    subscription = await store.subscribe(writer.turn_stream_id)

    def fail_offer(_: object) -> bool:
        raise RuntimeError("模拟 fanout 失败")

    monkeypatch.setattr(subscription, "offer", fail_offer)
    event = await writer.commit(
        "block.delta",
        {"block_id": "block_1", "operation": "append", "text": "已持久化"},
    )
    assert event["event_seq"] == 2
    restarted_store = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted_store.open(
        session_id=session_id,
        turn_id="job_fanout_failure",
    )
    restarted_state = await restarted_store.get_state(restarted_writer.turn_stream_id)
    assert restarted_state["snapshot_seq"] == 2
    assert restarted_state["blocks"][0]["text"] == "已持久化"


@pytest.mark.asyncio
async def test_snapshot_control_frame_uses_checkpoint_high_water_without_new_event(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, _, session_id = message_stream_store
    writer = await store.open(
        session_id=session_id,
        turn_id="job_snapshot_high_water",
        job_id="job_snapshot_high_water",
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "block_1",
            "carrier_type": "text",
            "operation": "append",
            "text": "已提交",
        },
    )
    before = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    snapshot = await writer.snapshot()
    after = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )

    assert snapshot["type"] == "stream.snapshot"
    assert snapshot["event_seq"] == snapshot["payload"]["snapshot_seq"] == 2
    assert [event["event_seq"] for event in after] == [event["event_seq"] for event in before]
    assert snapshot["job_id"] == "job_snapshot_high_water"


@pytest.mark.asyncio
async def test_activity_lifecycle_is_replayable_and_snapshot_safe(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_activity_lifecycle")
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())

    await runtime.started(
        activity_id="activity_1",
        kind="resource.operation",
        summary="启动开发服务",
        resource_refs=("dev_server_1",),
        resumable=True,
    )
    await runtime.updated(
        activity_id="activity_1",
        kind="resource.operation",
        status="waiting",
        summary="等待服务就绪",
    )
    await runtime.failed(
        activity_id="activity_1",
        kind="resource.operation",
        outcome="outcome_unknown",
        summary="后端重启，服务状态未知",
    )

    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    assert [event["type"] for event in events] == [
        "stream.opened",
        "activity.started",
        "activity.updated",
        "activity.failed",
    ]
    snapshot = await writer.snapshot()
    assert snapshot["payload"]["activities"][0]["status"] == "failed"
    assert snapshot["payload"]["activities"][0]["outcome"] == "outcome_unknown"

    restarted = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted.open_existing(
        session_id=session_id,
        turn_id="job_activity_lifecycle",
        turn_stream_id=writer.turn_stream_id,
    )
    restarted_snapshot = await restarted_writer.snapshot()
    assert restarted_snapshot["payload"]["activities"][0]["detail_available"] is False


@pytest.mark.asyncio
async def test_repeated_compaction_keeps_ordered_lifecycles_across_snapshot_and_restart(
    message_stream_store: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_repeated_compaction")
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())

    await writer.commit(
        "model.started",
        {"model_call_id": "model_1", "attempt": 1, "model": "primary"},
        model_call_id="model_1",
    )
    await writer.commit(
        "block.started",
        {"block_id": "block_1", "block_index": 0, "carrier_type": "text"},
        model_call_id="model_1",
        block_id="block_1",
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "block_1",
            "carrier_type": "text",
            "operation": "append",
            "text": "第一段",
        },
        model_call_id="model_1",
        block_id="block_1",
    )
    await writer.commit(
        "block.completed",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "text",
            "status": "completed",
        },
        model_call_id="model_1",
        block_id="block_1",
    )
    await writer.commit(
        "model.completed",
        {"model_call_id": "model_1", "attempt": 1, "outcome": "accepted"},
        model_call_id="model_1",
    )
    await runtime.started(
        activity_id="compaction_1",
        kind="context.compaction",
        summary="第一次压缩",
    )
    await runtime.completed(
        activity_id="compaction_1",
        kind="context.compaction",
        summary="第一次压缩完成",
    )
    await writer.commit(
        "model.started",
        {"model_call_id": "model_2", "attempt": 2, "model": "primary"},
        model_call_id="model_2",
    )
    await writer.commit(
        "block.started",
        {"block_id": "block_2", "block_index": 0, "carrier_type": "reasoning"},
        model_call_id="model_2",
        block_id="block_2",
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "block_2",
            "carrier_type": "reasoning",
            "operation": "append",
            "text": "第二段思考",
        },
        model_call_id="model_2",
        block_id="block_2",
    )
    await runtime.started(
        activity_id="compaction_2",
        kind="context.compaction",
        summary="第二次压缩",
    )

    running_snapshot = await writer.snapshot()
    assert running_snapshot["event_seq"] == running_snapshot["payload"]["snapshot_seq"] == 12
    running_activities = running_snapshot["payload"]["activities"]
    assert [activity["activity_id"] for activity in running_activities] == [
        "compaction_1",
        "compaction_2",
    ]
    assert running_activities[0]["status"] == "completed"
    assert running_activities[1]["status"] == "running"
    assert running_activities[0]["started_seq"] < running_activities[1]["started_seq"]
    assert running_activities[0]["completed_seq"] < running_activities[1]["started_seq"]
    assert running_snapshot["payload"]["model_calls"][0]["completed_seq"] == 6
    assert running_snapshot["payload"]["model_calls"][1]["started_seq"] == 9
    assert running_snapshot["payload"]["blocks"][0]["last_event_seq"] < running_snapshot["payload"]["blocks"][1]["started_seq"]

    await runtime.updated(
        activity_id="compaction_2",
        kind="context.compaction",
        summary="第二次压缩完成",
    )
    await runtime.completed(
        activity_id="compaction_2",
        kind="context.compaction",
        summary="第二次压缩完成",
    )
    await writer.commit(
        "block.completed",
        {
            "block_id": "block_2",
            "block_index": 0,
            "carrier_type": "reasoning",
            "status": "completed",
        },
        model_call_id="model_2",
        block_id="block_2",
    )
    await writer.commit(
        "model.completed",
        {"model_call_id": "model_2", "attempt": 2, "outcome": "accepted"},
        model_call_id="model_2",
    )

    final_snapshot = await writer.snapshot()
    assert final_snapshot["event_seq"] == final_snapshot["payload"]["snapshot_seq"] == 16
    assert [activity["activity_id"] for activity in final_snapshot["payload"]["activities"]] == [
        "compaction_1",
        "compaction_2",
    ]
    assert all(
        activity["completed_seq"] is not None
        for activity in final_snapshot["payload"]["activities"]
    )

    restarted = MessageStreamStore(path_resolver=resolver)
    restarted_writer = await restarted.open_existing(
        session_id=session_id,
        turn_id="job_repeated_compaction",
        turn_stream_id=writer.turn_stream_id,
    )
    replayed_snapshot = await restarted_writer.snapshot()
    assert replayed_snapshot["event_seq"] == 16
    replayed_activities = replayed_snapshot["payload"]["activities"]
    assert [activity["activity_id"] for activity in replayed_activities] == [
        "compaction_1",
        "compaction_2",
    ]
    assert replayed_activities[0]["started_seq"] < replayed_activities[1]["started_seq"]
    assert replayed_activities[0]["updated_at"]
    assert replayed_activities[1]["completed_at"]
