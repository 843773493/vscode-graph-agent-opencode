"""真实任务准入后的 wakeup 失败事实、原 Job 身份与生命周期恢复。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping

import pytest

from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
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
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离真实工作区
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["job-dispatch", "dispatch-receipt"])
async def test_wakeup_failure_retains_original_admission_and_recovers_once(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, stage: str,
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target, "context_and_run"))
    dispatcher, saver = case.container.runtime_source_dispatcher, case.container.checkpointer
    jobs = case.container.job_service
    assert isinstance(jobs, JobService)
    entered, release = asyncio.Event(), asyncio.Event()
    original_run = jobs._job_executor.run
    original_finish = saver.finish_runtime_wakeup
    native_locations = ("/private/wakeup-store.sqlite", r"C:\private\wakeup-store.sqlite", r"\\target\private\wakeup-store.sqlite")
    raw_error = ValueError("原始派发失败: " + " | ".join(native_locations))

    async def hold_before_provider(job: JobRuntimeStateProtocol) -> str:
        if job.session_id == session_id:
            entered.set()
            await release.wait()
        return await original_run(job)

    async def fail_dispatch(*_args: object, **_kwargs: object) -> None:
        raise raw_error

    def fail_receipt(
        wakeup: RuntimeExecutionWakeup, *, dispatch_generation: int,
        receipt: Mapping[str, object] | None = None, error: str | None = None,
    ) -> None:
        if receipt is not None:
            raise raw_error
        original_finish(wakeup, dispatch_generation=dispatch_generation, receipt=receipt, error=error)

    monkeypatch.setattr(jobs._job_executor, "run", hold_before_provider)
    await case.plan(session_id, [{"content": "原 wakeup 恢复完成。"}])
    try:
        with monkeypatch.context() as fault, caplog.at_level(logging.ERROR):
            if stage == "job-dispatch":
                fault.setattr(jobs, "start_job", fail_dispatch)
            else:
                fault.setattr(saver, "finish_runtime_wakeup", fail_receipt)
            run = await _check(case, task)
            failed = await _run(case, task, run, "failed")
            wakeup = dispatcher.submitted(session_id, thread_id, idempotency_key=run["run_id"])
            assert wakeup is not None and wakeup.request_id == run["request_id"]
            canonical = saver._storage.executions.runtime_wakeup_input(wakeup)
            admissions = case.sql(session_id, thread_id,
                "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome FROM executions")
            assert len(admissions) == 1 and admissions[0][:3] == (
                wakeup.execution_id, wakeup.job_id, wakeup.admission_idempotency_key,
            ) and admissions[0][-1] == "unknown"
            state, generation, detail, receipt = case.sql(session_id, thread_id,
                "SELECT state,dispatch_generation,last_error,dispatch_receipt_json FROM runtime_execution_wakeups")[0]
            assert (state, generation, receipt) == ("pending", 1, None)
            assert "wakeup-store.sqlite" not in detail
            assert f"stage={stage}" in detail and "error=ValueError" in detail
            assert all(identity in detail for identity in (
                session_id, thread_id, wakeup.job_id, wakeup.execution_id, wakeup.admission_idempotency_key,
            )) and "dispatch_generation=1" in detail
            failure = detail
            assert any(record.name == "app.core.failures" and record.exc_info is not None
                       and record.exc_info[1] is raw_error for record in caplog.records)
            assert all(location in caplog.text for location in native_locations)
            original_job = jobs._jobs.get(wakeup.job_id)
            if stage == "job-dispatch":
                assert original_job is None
            else:
                await asyncio.wait_for(entered.wait(), 30)
                assert original_job is not None and original_job.task is not None
            original_task = None if original_job is None else original_job.task
            before_context = (get_current_request_id(), get_current_gateway_id())
            with pytest.raises(ValueError) as caught:
                await dispatcher.dispatch(wakeup)
            assert caught.value is raw_error
            assert (get_current_request_id(), get_current_gateway_id()) == before_context
            assert case.sql(session_id, thread_id,
                "SELECT state,dispatch_generation FROM runtime_execution_wakeups") == [("pending", 2)]
            assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM model_calls") == [(0,)]

        # 生命周期只消费原持久 wakeup，不替任务创建准入或改写原失败运行。
        await dispatcher.start()
        await asyncio.wait_for(entered.wait(), 30)
        recovered_job = jobs._jobs[wakeup.job_id]
        if original_job is not None:
            assert recovered_job is original_job and recovered_job.task is original_task
        recovered_task = recovered_job.task
        assert recovered_task is not None and not recovered_task.done()
        repeated = await dispatcher.dispatch(wakeup)
        assert repeated.job_id == wakeup.job_id and recovered_job.task is recovered_task
        assert case.sql(session_id, thread_id,
            "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash,outcome FROM executions") == admissions
        assert saver._storage.executions.runtime_wakeup_input(wakeup) == canonical
        state, generation, detail, receipt = case.sql(session_id, thread_id,
            "SELECT state,dispatch_generation,last_error,dispatch_receipt_json FROM runtime_execution_wakeups")[0]
        assert (state, generation, detail) == ("dispatched", 4, None)
        assert json.loads(receipt)["job_id"] == wakeup.job_id
    finally:
        release.set()

    await _job(case, wakeup.job_id)
    terminal = await dispatcher.dispatch(wakeup)
    assert terminal.job_id == wakeup.job_id and terminal.status == "completed"
    assert len(await jobs.list(session_id=session_id)) == 1
    assert case.sql(session_id, thread_id,
        "SELECT execution_id,job_id,admission_idempotency_key,admission_payload_hash FROM executions") == [admissions[0][:-1]]
    assert case.sql(session_id, thread_id,
        "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='runtime_notice'") == [(1,)]
    assert len(case.requests()) == 1
    trace = [event.type for event in case.container.trace_event_store.read_events(session_id) if event.job_id == wakeup.job_id]
    assert trace.count("job_created") == trace.count("job_started") == 1
    response = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{run['run_id']}")
    assert response.status_code == 200 and response.json()["data"]["run"] == failed
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert "wakeup-store.sqlite" not in response.text
    case.write_actual(f"wakeup-failure-{stage}", {
        "failed_run": failed, "wakeup_error": failure, "terminal": terminal.model_dump(mode="json"),
    })
