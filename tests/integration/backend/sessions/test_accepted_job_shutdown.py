"""真实任务准入后装载失败，原 Job 仍可取消、重试和关闭。"""

from __future__ import annotations

import sqlite3

import pytest

from app import main
from app.core.path_utils import workspace_context
from app.schemas.internal_v2.common import ControlAction, JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_persistent_tasks import (
    _check,
    _configuration,
    _create,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前文件隔离产品工作区。
)


@pytest.mark.parametrize("operation", ["cancel", "shutdown", "shutdown-write-failure", "retry"])
async def test_accepted_job_survives_projection_failure(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target, "context_and_run"))
    jobs = case.container.job_service
    assert isinstance(jobs, JobService)
    assert jobs._bus is not None
    saver, dispatcher = case.container.checkpointer, case.container.runtime_source_dispatcher
    backend = main.app.state.workspace_lifespan
    raw_error = ValueError("注入已接受 Job 的队列装载失败")

    def fail_projection(*_args: object, **_kwargs: object) -> str:
        raise raw_error

    await case.plan(session_id, [{"content": "原接受项经唯一队列重试完成。"}])
    try:
        with monkeypatch.context() as fault:
            fault.setattr(jobs._input_owner, "accepted_job_outcome", fail_projection)
            run = await _check(case, task)
            failed = await _run(case, task, run, "failed")
        wakeup = dispatcher.submitted(session_id, thread_id, idempotency_key=run["run_id"])
        assert wakeup is not None
        job = jobs._jobs[wakeup.job_id]
        assert job.task is None and jobs._thread_current_job == {}
        assert jobs._pending_queue.queued_job_ids(session_id) == ()
        identity = case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions")
        assert len(identity) == 1 and identity[0][:3] == (
            wakeup.execution_id, wakeup.job_id, wakeup.admission_idempotency_key,
        )
        assert saver.runtime_execution_outcome(session_id, thread_id, wakeup.execution_id) == "unknown"
        database = saver._storage.storage.root(session_id, thread_id=thread_id) / "index.sqlite"
        with workspace_context(case.container.workspace_id, case.workspace):
            assert len(await jobs.drain_blockers()) == 1
        assert case.call_count(session_id, thread_id) == 0

        if operation.startswith("shutdown"):
            if operation == "shutdown-write-failure":
                write_error = OSError("注入原 Job 的取消终态写入失败")

                def fail_terminal(**_kwargs: object) -> bool:
                    raise write_error

                with monkeypatch.context() as fault:
                    fault.setattr(saver, "mark_job_terminal_status", fail_terminal)
                    with pytest.raises(BaseExceptionGroup) as caught:
                        await backend.close()
                assert caught.value.exceptions == (write_error,)
                assert not backend.closed and main.app.state.workspace_lifespan is backend
                assert jobs._jobs[wakeup.job_id] is job and job.status == JobStatus.accepted
                assert job.task is None and job.ended_at is None
                assert saver.runtime_execution_outcome(session_id, thread_id, wakeup.execution_id) == "unknown"
            await backend.close()
            assert backend.closed and main.app.state.workspace_lifespan is None
        elif operation == "cancel":
            response = await jobs.control(wakeup.job_id, JobControlRequest(action=ControlAction.cancel))
            assert response.status == JobStatus.cancelled
        else:
            await dispatcher.dispatch(wakeup)
            await _job(case, wakeup.job_id)
            assert jobs._jobs[wakeup.job_id] is job
            assert len(case.requests()) == 1 and case.call_count(session_id, thread_id) == 1
            assert case.sql(session_id, thread_id,
                "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions") == identity

        if operation != "retry":
            assert job.status == JobStatus.cancelled and job.ended_at is not None
            assert jobs._pending_queue.queued_job_ids(session_id) == ()
            assert await jobs.drain_blockers() == [] and jobs._thread_current_job == {}
            assert jobs._pending_queue.snapshot_version(session_id) > 0
            assert case.call_count(session_id, thread_id) == 0 and case.requests() == []
            if operation.startswith("shutdown"):
                events = await jobs._bus.list_events(wakeup.job_id)
                assert [event.type for event in events].count("job_cancelled") == 1
                assert all(event.type != "session_interrupted" for event in events)
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as cold:
                assert cold.execute("SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome FROM executions").fetchall() == [(*identity[0], "cancelled")]
            if operation == "cancel":
                await dispatcher.dispatch(wakeup)
                assert jobs._jobs[wakeup.job_id] is job and job.task is None
                assert case.call_count(session_id, thread_id) == 0
        case.write_actual(f"accepted-{operation}", {"identity": identity, "failed_run": failed, "status": job.status.value})
    finally:
        # 红测也沿原装载/关闭入口结清，避免残留 Backend 污染后续用例。
        if not backend.closed:
            try:
                await backend.close()
            finally:
                if not backend.closed:
                    await backend.close()
