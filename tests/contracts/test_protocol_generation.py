from app.protocol.generated.boxteam.workspace.v2 import (
    job_pb2,
    public_pb2,
    session_interaction_pb2,
)


def test_python_binding_preserves_cross_file_import_and_oneof() -> None:
    event = session_interaction_pb2.SessionExecutionEvent(
        type="job.updated",
        header=session_interaction_pb2.SessionExecutionEventHeader(
            event_id="event_123",
            session_id="session_123",
        ),
        job_updated=job_pb2.JobProgress(
            job_id="job_123",
            status=job_pb2.JOB_STATUS_RUNNING,
            progress=42,
        ),
    )

    assert event.WhichOneof("payload") == "job_updated"
    assert event.job_updated.progress == 42


def test_current_job_and_pending_request_owner_fields_keep_distinct_wire_tags() -> None:
    for message_type in (job_pb2.Job, public_pb2.JobDTO):
        field = message_type.DESCRIPTOR.fields_by_name["thread_id"]
        assert field.number == 14
        value = message_type(job_id="job_fixture", thread_id="thr_fixture")
        decoded = message_type.FromString(value.SerializeToString())
        assert decoded.thread_id == "thr_fixture"
    fields = public_pb2.PendingRequestDTO.DESCRIPTOR.fields_by_name
    assert fields["thread_id"].number == 19
    assert fields["turn_id"].number == 20
    assert fields["execution_binding"].number == 21
    pending = public_pb2.PendingRequestDTO(thread_id="thr_fixture")
    assert not pending.HasField("turn_id")
    pending.turn_id = "turn_fixture"
    decoded = public_pb2.PendingRequestDTO.FromString(pending.SerializeToString())
    assert decoded.HasField("turn_id") and decoded.turn_id == "turn_fixture"
