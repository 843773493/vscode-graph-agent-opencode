from datetime import UTC, datetime

import pytest

from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.protocol.codecs.session_sse import session_sse_to_json, session_sse_to_proto
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2
from app.protocol.generated.boxteam.workspace.v2 import session_stream_pb2
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.session_interaction import (
    JobProgressDTO,
    JobUpdatedExecutionEventDTO,
    MessageObservationDTO,
    MessageUpdatedExecutionEventDTO,
    SessionExecutionSseDTO,
)


@pytest.mark.parametrize("event_type", ["job.updated", "session.error", "unknown.event", ""])
def test_session_decoder_rejects_discriminator_payload_mismatch(event_type: str) -> None:
    wire = session_stream_pb2.SessionExecutionSse()
    wire.event.type = event_type
    wire.event.header.SetInParent()
    wire.event.message_updated.message_id = "msg_discriminator"
    original = wire.SerializeToString()
    with pytest.raises(ValueError, match="type 与 payload 不一致"):
        session_sse_to_json(session_stream_pb2.SessionExecutionSse.FromString(original))
    assert wire.SerializeToString() == original


@pytest.mark.parametrize("event_type", ["stream.opened", "tool_call.delta", "activity.failed", "unknown.event", ""])
def test_message_decoder_rejects_discriminator_payload_mismatch(event_type: str) -> None:
    wire = message_stream_pb2.MessageStreamEvent(type=event_type)
    wire.block_delta.text = "增量 Ω"
    original = wire.SerializeToString()
    with pytest.raises(ValueError, match="type 与 payload 不一致"):
        message_stream_to_json(message_stream_pb2.MessageStreamEvent.FromString(original))
    assert wire.SerializeToString() == original


@pytest.mark.parametrize(("event_type", "payload", "payload_name"), [
    ("job.step.updated", {}, "job_step_updated"),
    ("job.step.updated", {"agent_id": "", "message": "进度 Ω", "phase": "tool"}, "job_step_updated"),
    ("session.error", {"error": ""}, "session_error"),
    ("session.error", {"error": "执行失败 Ω"}, "session_error"),
    ("trace.observed", {"raw_type": ""}, "trace_observed"),
    ("trace.observed", {"raw_type": "llm_request"}, "trace_observed"),
    ("session.status.changed", {"session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea", "status": "idle"},
     "session_status_changed"),
    ("session.status.changed", {"session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea", "status": "retry",
                                "message": "", "active_job_id": "job_1", "waiting": {
                                    "id": "wait_1", "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
                                    "message": "等待网络", "restored": False, "created_at": "2026-10-30T00:00:00Z",
                                }}, "session_status_changed"),
    ("session.status.changed", {"session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
                                "is_streaming": True, "is_idle": False}, "session_status_changed"),
])
def test_session_payload_wire_preserves_empty_messages_and_business_status(
    event_type: str, payload: dict[str, object], payload_name: str,
) -> None:
    source = SessionExecutionSseDTO.model_validate({
        "event": {"event_id": "evt_payload", "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
                  "time": "2026-10-30T00:00:00Z", "type": event_type, "payload": payload},
        "raw_type": "原始事件", "raw_payload": {"nested": {"value": [False, None, "Ω"]}},
    })
    wire = session_stream_pb2.SessionExecutionSse.FromString(session_sse_to_proto(source).SerializeToString())
    assert wire.event.WhichOneof("payload") == payload_name
    assert session_sse_to_json(wire) == source.model_dump(mode="json")


@pytest.mark.parametrize("event_type", ["job.updated", "job.status.changed", "session.completed"])
@pytest.mark.parametrize("status", list(JobStatus))
def test_job_progress_wire_preserves_status_defaults_and_optional_values(event_type: str, status: JobStatus) -> None:
    for optional in ({}, {"message": "", "current_step_id": ""},
                     {"message": "执行中 Ω", "current_step_id": "step_1", "progress": 42}):
        source = SessionExecutionSseDTO.model_validate({
            "event": {"event_id": "evt_job", "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
                      "time": "2026-10-30T00:00:00Z", "type": event_type,
                      "payload": {"job_id": "job_1", "status": status.value, **optional}},
            "raw_type": "job_status_changed", "raw_payload": {"status": status.value},
        })
        wire = session_stream_pb2.SessionExecutionSse.FromString(session_sse_to_proto(source).SerializeToString())
        payload_name = wire.event.WhichOneof("payload")
        assert payload_name == event_type.replace(".", "_")
        payload = getattr(wire.event, payload_name)
        assert payload.HasField("message") == ("message" in optional)
        assert payload.HasField("current_step_id") == ("current_step_id" in optional)
        assert session_sse_to_json(wire) == source.model_dump(mode="json")


def test_child_message_sse_round_trip_preserves_owner_and_lifecycle_time() -> None:
    created_at = datetime(2026, 10, 14, 11, 59, tzinfo=UTC)
    published_at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    value = SessionExecutionSseDTO(
        event=MessageUpdatedExecutionEventDTO(
            event_id="evt_child_message",
            session_id="ses_019bef80066f7038ae8e88ccb2e5e2ea",
            job_id="job_child_message",
            time=published_at,
            type="message.updated",
            payload=MessageObservationDTO(
                message_id="msg_child",
                session_id="ses_019bef80066f7038ae8e88ccb2e5e2ea",
                thread_id="thr_child",
                role="user",
                content="child input",
                created_at=created_at,
            ),
        ),
        raw_type="message_created",
        raw_payload={"thread_id": "thr_child"},
    )

    encoded = session_sse_to_proto(value)
    assert encoded.event.WhichOneof("payload") == "message_updated"
    assert encoded.event.message_updated.thread_id == "thr_child"
    decoded = session_sse_to_json(encoded)
    event = decoded["event"]
    assert isinstance(event, dict)
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["thread_id"] == "thr_child"
    assert payload["created_at"] == "2026-10-14T11:59:00Z"
    assert event["time"] == "2026-10-14T12:00:00Z"


def test_session_sse_codec_preserves_oneof_and_dynamic_payload() -> None:
    value = SessionExecutionSseDTO(
        event=JobUpdatedExecutionEventDTO(
            event_id="event_123",
            session_id="ses_019bef80066f7038ae8e88ccb2e5e2ea",
            job_id="job_123",
            time=datetime(2026, 8, 24, tzinfo=UTC),
            type="job.updated",
            payload=JobProgressDTO(
                job_id="job_123",
                status=JobStatus.running,
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
    event = decoded["event"]
    assert isinstance(event, dict)
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["progress"] == 42
    assert payload["message"] == "working"


def test_message_stream_snapshot_preserves_tool_call_arguments() -> None:
    encoded = message_stream_to_proto(
        {
            "event_id": "event_123",
            "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "turn_id": "turn_123",
            "thread_id": "thr_00000000400070008000000000000002",
        "turn_stream_id": "stream_123",
            "event_seq": 12,
            "type": "stream.snapshot",
            "workspace_id": "wsp_123",
            "payload": {
                "workspace_id": "wsp_123",
                "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
                "turn_id": "turn_123",
                "thread_id": "thr_00000000400070008000000000000002",
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
                        "model_call_id": "model_123",
                        "tool_invocation_id": "invocation_123",
                        "tool_attempt_id": "attempt_123",
                    }
                ],
                "tool_executions": [
                    {
                        "tool_execution_id": "execution_123",
                        "tool_call_id": "call_123",
                        "tool_name": "shell",
                        "status": "completed",
                        "outcome": "success",
                        "result": "/workspace",
                        "model_call_id": "model_123",
                    }
                ],
                "resumable": True,
            },
        }
    )

    assert encoded.stream_snapshot.workspace_id == "wsp_123"
    assert encoded.stream_snapshot.tool_calls[0].tool_call_id == "call_123"
    assert encoded.stream_snapshot.tool_calls[0].tool_invocation_id == "invocation_123"
    assert encoded.stream_snapshot.tool_calls[0].tool_attempt_id == "attempt_123"
    decoded = message_stream_to_json(encoded)
    snapshot = decoded["payload"]
    assert snapshot["snapshot_seq"] == 12
    assert isinstance(snapshot["snapshot_seq"], int)
    assert snapshot["tool_calls"][0]["arguments"] == {"command": "pwd"}
    assert "model_call_id" not in snapshot["tool_calls"][0]
    assert "model_call_id" not in snapshot["tool_executions"][0]
    assert snapshot["workspace_id"] == "wsp_123"


def test_message_stream_codec_projects_internal_tool_and_model_fields() -> None:
    encoded_delta = message_stream_to_proto(
        {
            "event_id": "event_tool_delta",
            "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "turn_id": "turn_123",
            "thread_id": "thr_00000000400070008000000000000002",
        "turn_stream_id": "stream_123",
            "event_seq": 13,
            "type": "tool_call.delta",
            "workspace_id": "wsp_123",
            "tool_call_id": "call_123",
            "tool_invocation_id": "invocation_123",
            "payload": {
                "tool_call_id": "call_123",
                "tool_invocation_id": "invocation_123",
                "tool_name": "shell",
                "arguments_delta": "{\"command\":\"pwd\"}",
                "status": "streaming",
            },
        }
    )
    delta = message_stream_to_json(encoded_delta)["payload"]
    assert encoded_delta.workspace_id == "wsp_123"
    assert encoded_delta.tool_call_id == "call_123"
    assert encoded_delta.tool_invocation_id == "invocation_123"
    assert delta["arguments_delta"] == '{"command":"pwd"}'
    assert message_stream_to_json(encoded_delta)["tool_call_id"] == "call_123"
    assert message_stream_to_json(encoded_delta)["tool_invocation_id"] == "invocation_123"
    assert "status" not in delta

    encoded_snapshot = message_stream_to_proto(
        {
            "event_id": "event_snapshot_projection",
            "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "turn_id": "turn_123",
            "thread_id": "thr_00000000400070008000000000000002",
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
                        "tool_invocation_id": "invocation_123",
                        "tool_attempt_id": "attempt_123",
                    }
                ],
                "workspace_id": "wsp_123",
            },
        }
    )
    assert encoded_snapshot.stream_snapshot.workspace_id == "wsp_123"
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
    assert snapshot["tool_executions"][0]["tool_invocation_id"] == "invocation_123"
    assert snapshot["tool_executions"][0]["tool_attempt_id"] == "attempt_123"


def test_message_stream_v1_round_trip_preserves_terminal_projection() -> None:
    encoded = message_stream_to_proto(
        {
            "event_id": "event_terminal",
            "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "turn_id": "turn_123",
            "thread_id": "thr_00000000400070008000000000000002",
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
