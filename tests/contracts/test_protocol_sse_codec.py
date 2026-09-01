from datetime import UTC, datetime

from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.protocol.codecs.session_sse import session_sse_to_json, session_sse_to_proto
from app.schemas.internal_v2.session_interaction import (
    JobProgressDTO,
    JobUpdatedExecutionEventDTO,
    SessionExecutionSseDTO,
)


def test_session_sse_codec_preserves_oneof_and_dynamic_payload() -> None:
    value = SessionExecutionSseDTO(
        event=JobUpdatedExecutionEventDTO(
            event_id="event_123",
            session_id="session_123",
            job_id="job_123",
            time=datetime(2026, 8, 24, tzinfo=UTC),
            type="job.updated",
            payload=JobProgressDTO(
                job_id="job_123",
                status="running",
                progress=42,
                message="working",
            ),
        ),
        raw_type="text_delta",
        raw_payload={"provider": {"request_id": "request_123"}},
    )

    encoded = session_sse_to_proto(value)
    assert encoded.event.WhichOneof("payload") == "job_updated"
    assert encoded.event.job_updated.progress == 42

    decoded = session_sse_to_json(encoded)
    assert decoded["raw_payload"] == {"provider": {"request_id": "request_123"}}
    assert decoded["event"]["payload"]["progress"] == 42
    assert decoded["event"]["payload"]["message"] == "working"


def test_message_stream_snapshot_preserves_tool_call_arguments() -> None:
    encoded = message_stream_to_proto(
        {
            "event_id": "event_123",
            "session_id": "session_123",
            "turn_id": "turn_123",
            "turn_stream_id": "stream_123",
            "event_seq": 12,
            "type": "stream.snapshot",
            "payload": {
                "session_id": "session_123",
                "turn_id": "turn_123",
                "turn_stream_id": "stream_123",
                "snapshot_seq": 12,
                "stream_status": "open",
                "agent_loop_status": "tool_running",
                "current_attempt": 1,
                "blocks": [],
                "tool_calls": [
                    {
                        "tool_call_id": "call_123",
                        "tool_name": "shell",
                        "arguments": {"command": "pwd"},
                        "status": "streaming",
                    }
                ],
                "tool_executions": [],
                "resumable": True,
            },
        }
    )

    assert encoded.stream_snapshot.tool_calls[0].tool_call_id == "call_123"
    decoded = message_stream_to_json(encoded)
    snapshot = decoded["payload"]
    assert snapshot["snapshot_seq"] == 12
    assert isinstance(snapshot["snapshot_seq"], int)
    assert snapshot["tool_calls"][0]["arguments"] == {"command": "pwd"}


def test_message_stream_codec_projects_internal_tool_and_model_fields() -> None:
    encoded_delta = message_stream_to_proto(
        {
            "event_id": "event_tool_delta",
            "session_id": "session_123",
            "turn_id": "turn_123",
            "turn_stream_id": "stream_123",
            "event_seq": 13,
            "type": "tool_call.delta",
            "payload": {
                "tool_call_id": "call_123",
                "tool_name": "shell",
                "arguments_delta": "{\"command\":\"pwd\"}",
                "status": "streaming",
            },
        }
    )
    delta = message_stream_to_json(encoded_delta)["payload"]
    assert delta["arguments_delta"] == '{"command":"pwd"}'
    assert "status" not in delta

    encoded_snapshot = message_stream_to_proto(
        {
            "event_id": "event_snapshot_projection",
            "session_id": "session_123",
            "turn_id": "turn_123",
            "turn_stream_id": "stream_123",
            "event_seq": 14,
            "type": "stream.snapshot",
            "payload": {
                "snapshot_seq": 14,
                "stream_status": "failed",
                "agent_loop_status": "failed",
                "model_calls": [
                    {
                        "model_call_id": "model_1",
                        "attempt": 1,
                        "status": "failed",
                        "outcome": "upstream_error",
                        "error_code": "provider_error",
                        "message": "上游失败详情",
                        "reason": "provider_error",
                    }
                ],
                "tool_calls": [
                    {
                        "tool_call_id": "call_123",
                        "tool_name": "shell",
                        "arguments": {"command": "pwd"},
                        "status": "completed",
                    }
                ],
                "tool_executions": [
                    {
                        "tool_execution_id": "exec_123",
                        "tool_call_id": "call_123",
                        "tool_name": "shell",
                        "status": "completed",
                        "outcome": "success",
                        "result": "/workspace",
                    }
                ],
            },
        }
    )
    snapshot = message_stream_to_json(encoded_snapshot)["payload"]
    assert snapshot["model_calls"] == [
        {
            "model_call_id": "model_1",
            "attempt": 1,
            "status": "failed",
            "outcome": "upstream_error",
        }
    ]
    assert snapshot["tool_calls"][0]["arguments"] == {"command": "pwd"}
    assert snapshot["tool_executions"][0]["result"] == "/workspace"


def test_message_stream_v1_round_trip_preserves_terminal_projection() -> None:
    encoded = message_stream_to_proto(
        {
            "event_id": "event_terminal",
            "session_id": "session_123",
            "turn_id": "turn_123",
            "turn_stream_id": "stream_123",
            "job_id": "job_123",
            "event_seq": 21,
            "type": "stream.snapshot",
            "payload": {
                "snapshot_seq": 21,
                "stream_status": "failed",
                "agent_loop_status": "failed",
                "current_attempt": 2,
                "blocks": [{
                    "block_id": "block_1",
                    "block_index": 0,
                    "carrier_type": "text",
                    "status": "interrupted",
                    "text": "半截",
                    "partial": True,
                    "completion_reason": "user_interrupt",
                    "items": [],
                    "redacted": False,
                    "projection": "streaming",
                    "started_seq": 4,
                    "last_event_seq": 8,
                    "completed_seq": 8,
                    "started_at": "2026-08-24T00:00:04Z",
                    "updated_at": "2026-08-24T00:00:08Z",
                    "completed_at": "2026-08-24T00:00:08Z",
                }],
                "tool_calls": [{
                    "tool_call_id": "call_1",
                    "tool_name": "shell",
                    "status": "incomplete",
                    "arguments_complete": False,
                    "started_seq": 9,
                    "last_event_seq": 10,
                    "completed_seq": 10,
                    "started_at": "2026-08-24T00:00:09Z",
                    "updated_at": "2026-08-24T00:00:10Z",
                    "completed_at": "2026-08-24T00:00:10Z",
                }],
                "tool_executions": [{
                    "tool_execution_id": "exec_1",
                    "tool_call_id": "call_1",
                    "tool_name": "shell",
                    "status": "completed",
                    "outcome": "outcome_unknown",
                    "completion_reason": "execution_lost",
                    "started_seq": 11,
                    "last_event_seq": 21,
                    "completed_seq": 21,
                    "started_at": "2026-08-24T00:00:11Z",
                    "updated_at": "2026-08-24T00:00:21Z",
                    "completed_at": "2026-08-24T00:00:21Z",
                }],
                "model_calls": [{
                    "model_call_id": "model_1",
                    "attempt": 2,
                    "status": "failed",
                    "outcome": "user_interrupt",
                    "retryable": False,
                    "started_seq": 2,
                    "last_event_seq": 7,
                    "completed_seq": 7,
                    "started_at": "2026-08-24T00:00:02Z",
                    "updated_at": "2026-08-24T00:00:07Z",
                    "completed_at": "2026-08-24T00:00:07Z",
                }],
                "activities": [{
                    "activity_id": "activity_1",
                    "kind": "browser.session",
                    "scope_ref": "session",
                    "status": "unknown",
                    "outcome": "execution_lost",
                    "completion_reason": "execution_lost",
                    "cancellable": False,
                    "resumable": False,
                    "side_effect_policy": "external",
                    "resource_refs": ["resource_1"],
                    "detail_available": False,
                    "started_seq": 12,
                    "last_event_seq": 20,
                    "completed_seq": 20,
                    "started_at": "2026-08-24T00:00:12Z",
                    "updated_at": "2026-08-24T00:00:20Z",
                    "completed_at": "2026-08-24T00:00:20Z",
                }],
                "resource_refs": [{
                    "resource_id": "resource_1",
                    "status": "unknown",
                }],
                "active_state": {
                    "kind": "terminal",
                    "phase": "failed",
                    "entity_id": "stream_123",
                    "status": "failed",
                },
                "interrupt_state": {
                    "request_id": "intr_1",
                    "status": "requested",
                    "fact_confirmed": False,
                },
                "recovery": {
                    "status": "execution_lost",
                    "code": "execution_lost",
                    "resumable": False,
                },
                "resumable": False,
            },
        }
    )

    decoded = message_stream_to_json(encoded)
    snapshot = decoded["payload"]
    assert decoded["job_id"] == "job_123"
    assert snapshot["blocks"][0]["partial"] is True
    assert snapshot["blocks"][0]["started_seq"] == 4
    assert snapshot["blocks"][0]["completed_seq"] == 8
    assert snapshot["blocks"][0]["completed_at"] == "2026-08-24T00:00:08Z"
    assert snapshot["tool_calls"][0]["last_event_seq"] == 10
    assert snapshot["tool_executions"][0]["outcome"] == "outcome_unknown"
    assert snapshot["tool_executions"][0]["started_at"] == "2026-08-24T00:00:11Z"
    assert snapshot["model_calls"][0]["outcome"] == "user_interrupt"
    assert snapshot["model_calls"][0]["completed_seq"] == 7
    assert snapshot["activities"][0]["detail_available"] is False
    assert snapshot["activities"][0]["started_seq"] == 12
    assert snapshot["activities"][0]["completed_at"] == "2026-08-24T00:00:20Z"
    assert "completion_reason" not in snapshot["activities"][0]
    assert snapshot["active_state"]["phase"] == "failed"
    assert snapshot["interrupt_state"]["fact_confirmed"] is False
