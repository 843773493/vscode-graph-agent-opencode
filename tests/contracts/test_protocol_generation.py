from app.protocol.generated.boxteam.workspace.v2 import (
    job_pb2,
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
