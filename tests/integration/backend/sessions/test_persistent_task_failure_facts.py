"""持久任务的失败由真实准入生成，普通记录保留事实，原生位置只留诊断。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from app.core.exceptions import NotFoundError
from tests.integration.backend.sessions.test_persistent_task_execution_events import (
    _event_configuration,
    _event_runs,
)
from tests.integration.backend.sessions.test_persistent_task_models import (
    _answer,
    _begin,
    _model_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _check,
    _configuration,
    _control,
    _create,
    _release_match,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前文件隔离工作区。
)


@pytest.mark.parametrize("stage", [
    "delivery-reconcile", "context-admission", "execution-admission",
    "model-check-reconcile", "model-check-admission",
])
async def test_failed_admission_keeps_native_diagnostics_out_of_persistent_task_records(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, stage: str,
) -> None:
    case = source_case
    session_id, _thread_id, target = await _target(case)
    judge_id, _judge_thread, judge = await _target(case)
    configuration = (_model_configuration(target, judge) if stage.startswith("model-") else
                     _configuration(target, "context_and_run" if stage == "execution-admission" else "context_only"))
    if stage.startswith("model-"):
        configuration["action"]["delivery_mode"] = "context_only"
    task = await _create(case, configuration)
    runtime = case.container.persistent_task_runtime
    private_paths = ("/private/task-store.sqlite", r"C:\private\task-store.sqlite", r"\\target\private\task-store.sqlite")
    raw_detail = "原始准入失败: " + " | ".join(private_paths)
    raw_error = NotFoundError({"native_location": raw_detail}) if stage.endswith("reconcile") else ValueError(raw_detail)

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise raw_error

    async def fail_admission(*_args: object, **_kwargs: object) -> None:
        raise raw_error

    with monkeypatch.context() as fault, caplog.at_level(logging.ERROR):
        if stage == "delivery-reconcile":
            fault.setattr(runtime._dispatcher, "context_published", fail_read)
        elif stage == "model-check-reconcile":
            fault.setattr(runtime._dispatcher, "submitted", fail_read)
        else:
            method = "publish_runtime_context" if stage == "context-admission" else "submit_runtime_event"
            fault.setattr(runtime._orchestrator, method, fail_admission)
        run = await _check(case, task)
        failed = await _run(case, task, run, "failed")

    current_response = await case.client.get(f"{ROOT}/{task['task_id']}")
    assert current_response.status_code == 200, current_response.text
    assert current_response.headers["X-Request-ID"] == current_response.json()["request_id"]
    current = current_response.json()["data"]
    assert current["lifecycle"] == "needs_attention" and current["reason"] == failed["error"]
    assert failed["delivery"] is None and failed["evaluation"] is None
    assert failed["request_id"] == run["request_id"] and failed["run_id"] == run["run_id"]
    assert f"stage={stage}" in failed["error"]
    assert f"error={type(raw_error).__name__}" in failed["error"]
    assert task["task_id"] in failed["error"] and run["run_id"] in failed["error"]
    assert (judge if stage.startswith("model-") else target) in failed["error"]
    detail_response = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{run['run_id']}")
    assert detail_response.status_code == 200, detail_response.text
    assert detail_response.headers["X-Request-ID"] == detail_response.json()["request_id"]
    serialized = json.dumps([current, failed, detail_response.json()], ensure_ascii=False)
    assert all(path not in serialized for path in private_paths) and "task-store.sqlite" not in serialized
    with sqlite3.connect((case.workspace / ".boxteam/state/workspace.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT error FROM persistent_task_runs WHERE run_id=?", (run["run_id"],)).fetchone() == (failed["error"],)
        assert connection.execute("SELECT reason FROM persistent_tasks WHERE task_id=?", (task["task_id"],)).fetchone() == (failed["error"],)
    assert any(record.exc_info is not None and record.exc_info[1] is raw_error for record in caplog.records)
    assert private_paths[0] in caplog.text and "task-store.sqlite" in caplog.text
    assert case.requests() == []

    # 修复后由原管理入口恢复并建立新运行；原失败记录及其身份不得被重写。
    resumed = await _control(case, current, "resume")
    if stage.startswith("model-"):
        retried = await _begin(case, resumed, "retry")
        await case.plan(judge_id, [{"content": json.dumps(_answer(retried))}])
        _release_match(case, "retry")
    else:
        if stage == "execution-admission":
            await case.plan(session_id, [{"content": "原任务已通过正式入口恢复。"}])
        retried = await _check(case, resumed, "retry")
    delivered = await _run(case, resumed, retried)
    assert delivered["run_id"] != failed["run_id"] and delivered["error"] is None
    assert await _check(case, resumed, "retry") == delivered
    if stage == "execution-admission":
        await _job(case, delivered["delivery"]["job_id"])
    original = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{run['run_id']}")
    assert original.json()["data"]["run"] == failed
    case.write_actual(f"task-failure-{stage}", {"failed": failed, "current": current, "recovered": delivered})


async def test_execution_source_failure_preserves_committed_history_and_resumes_at_explicit_cursor(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, source = await _target(case)
    _target_id, _target_thread, target = await _target(case)
    task = await _create(case, _event_configuration(source, target))
    saver = case.container.checkpointer
    raw_error = NotFoundError({"native_location": "/private/completion-store.sqlite"})
    original_completions = saver.execution_completions

    def fail_completion(session_id: str, thread_id: str, *, after_commit_id: int | None):
        page = original_completions(session_id, thread_id, after_commit_id=after_commit_id)
        if page.records:
            raise raw_error
        return page

    with monkeypatch.context() as fault, caplog.at_level(logging.ERROR):
        fault.setattr(saver, "execution_completions", fail_completion)
        await case.plan(session_id, [{"content": "来源执行已经真实完成。"}])
        accepted = await _send(case, session_id, "完成来源工作。")
        accepted_job_id = accepted["job_id"]
        assert isinstance(accepted_job_id, str)
        await _job(case, accepted_job_id)
        async with asyncio.timeout(5):
            while True:
                response = await case.client.get(f"{ROOT}/{task['task_id']}")
                assert response.status_code == 200, response.text
                current = response.json()["data"]
                if current["lifecycle"] == "needs_attention":
                    break
                await asyncio.sleep(0.02)

    reason = current["reason"]
    assert "stage=execution-source" in reason and "error=NotFoundError" in reason
    assert task["task_id"] in reason and source in reason and "completion-store.sqlite" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert any(record.exc_info is not None and record.exc_info[1] is raw_error for record in caplog.records)
    with sqlite3.connect((case.workspace / ".boxteam/state/workspace.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT reason FROM persistent_tasks WHERE task_id=?", (task["task_id"],)).fetchone() == (reason,)
        assert connection.execute("SELECT COUNT(*) FROM persistent_task_runs WHERE task_id=?", (task["task_id"],)).fetchone() == (0,)
    before = saver.execution_completions(session_id, thread_id, after_commit_id=0)
    assert len(before.records) == 1 and before.records[0].job_id == accepted["job_id"]
    resumed = await _control(case, current, "resume")
    assert resumed["lifecycle"] == "active"
    await case.plan(session_id, [{"content": "显式恢复后完成新的来源工作。"}])
    following = await _send(case, session_id, "新的来源工作。")
    following_job_id = following["job_id"]
    assert isinstance(following_job_id, str)
    await _job(case, following_job_id)
    delivered, = await _event_runs(case, resumed, 1)
    assert delivered["trigger_payload"]["job_id"] == following["job_id"]
    after = saver.execution_completions(session_id, thread_id, after_commit_id=0)
    assert after.records[0] == before.records[0] and len(after.records) == 2
    case.write_actual("task-source-failure", {"failed": current, "resumed": resumed, "delivered": delivered})


async def test_model_result_read_failure_retains_accepted_evaluation_without_replaying_it(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    work_id, _work_thread, target = await _target(case)
    judge_id, judge_thread, judge = await _target(case)
    configuration = _model_configuration(target, judge)
    configuration["action"]["delivery_mode"] = "context_only"
    task = await _create(case, configuration)
    case.control.arm_match("judge-accepted", "task.check.model_accepted_before_record", {"task_id": task["task_id"]})
    run = await _begin(case, task, "judge-launch")
    await case.plan(judge_id, [{"content": json.dumps(_answer(run))}])
    _release_match(case, "judge-launch")
    receipt = await asyncio.to_thread(case.control.acknowledged_match, "judge-accepted")
    details = receipt["details"]
    assert isinstance(details, dict)
    job_id = details["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    raw_error = NotFoundError({"native_location": "/private/evaluation-store.sqlite"})
    saver = case.container.checkpointer

    def fail_result(*_args: object, **_kwargs: object) -> None:
        raise raw_error

    with monkeypatch.context() as fault, caplog.at_level(logging.ERROR):
        fault.setattr(saver, "runtime_execution_outcome", fail_result)
        _release_match(case, "judge-accepted")
        failed = await _run(case, task, run, "failed")
    assert failed["delivery"] is None and failed["evaluation"]["job_id"] == job_id
    assert failed["evaluation"]["execution_id"] == details["execution_id"]
    assert "stage=model-check-result" in failed["error"] and "error=NotFoundError" in failed["error"]
    assert run["run_id"] in failed["error"] and judge in failed["error"]
    assert "evaluation-store.sqlite" not in json.dumps(failed)
    assert any(record.exc_info is not None and record.exc_info[1] is raw_error for record in caplog.records)
    assert saver.runtime_execution_outcome(judge_id, judge_thread, failed["evaluation"]["execution_id"]) == "completed"
    assert await case.container.job_service.list(session_id=work_id) == []
    jobs = await case.container.job_service.list(session_id=judge_id)
    assert len(jobs) == 1 and jobs[0].job_id == failed["evaluation"]["job_id"]
    response = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{run['run_id']}")
    assert response.status_code == 200 and response.json()["data"]["run"] == failed
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
    assert current["lifecycle"] == "needs_attention" and current["reason"] == failed["error"]
    case.write_actual("task-model-result-failure", {"failed": failed, "current": current})
