"""模型判断使用真实任务/执行准入和canonical结果，覆盖迟到、隔离与硬重启。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

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
    ModelReplayCase,
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 当前文件独立工作区
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 当前文件独立工作区
)


def _model_configuration(target: str, judge: str) -> dict:
    configuration = _configuration(target, "context_and_run")
    configuration["condition"] = {"version": "1", "type_id": "builtin.model", "script": None, "rule": None,
                                  "model": {"thread_uri": judge, "instruction": "根据本次输入判断是否继续。"}}
    return configuration


def _answer(run: dict, decision: str = "proceed") -> dict:
    return {"run_id": run["run_id"], "definition_revision": run["definition_revision"],
            "decision": decision, "reason": f"本次判断-{decision}", "result": {"evaluated": run["trigger_key"]}}


async def _begin(case: ModelReplayCase, task: dict, key: str, *, check_only: bool = False) -> dict:
    case.control.arm_match(key, "task.check.started_before_launch", {"task_id": task["task_id"]})
    run = await _check(case, task, key, check_only=check_only)
    await asyncio.to_thread(case.control.acknowledged_match, key)
    return run


async def _evaluation(case: ModelReplayCase, task: dict, run: dict) -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
            assert response.status_code == 200, response.text
            actual, = [row for row in response.json()["data"]["items"] if row["run_id"] == run["run_id"]]
            if actual["evaluation"] is not None:
                return actual["evaluation"]
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_all_model_decisions_use_explicit_execution_and_only_proceed_runs_work(source_case: RuntimeSourceCase) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    for decision in ["wait", "proceed", "complete", "needs_attention"]:
        task = await _create(case, _model_configuration(work_uri, judge_uri), decision)
        run = await _begin(case, task, decision)
        answer = _answer(run, decision)
        judge_call, = await case.plan(judge_id, [{"content": json.dumps(answer)}])
        work_calls = await case.plan(work_id, [{"content": "只处理通过的工作。"}]) if decision == "proceed" else []
        _release_match(case, decision)
        actual = await _run(case, task, run, "delivered" if decision == "proceed" else "checked")
        assert actual["decision"] == {key: answer[key] for key in ["decision", "reason", "result"]}
        evaluation = actual["evaluation"]
        wakeup = case.container.runtime_source_dispatcher.submitted(judge_id, judge_thread,
            idempotency_key=f"persistent-task-check:{run['run_id']}")
        assert wakeup is not None
        assert evaluation == {"thread_uri": task["configuration"]["condition"]["model"]["thread_uri"],
            "source_item_id": wakeup.source_item_id, "job_id": wakeup.job_id, "execution_id": wakeup.execution_id}
        request = _wire_text(case.request(judge_call))
        assert run["run_id"] in request and task["configuration"]["context"]["content"] in request
        assert "只返回一个JSON对象" in request
        assert actual["request_id"] == wakeup.request_id
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["lifecycle"] == (decision if decision in {"complete", "needs_attention"} else "active")
        if decision == "proceed":
            await _job(case, actual["delivery"]["job_id"])
            assert run["run_id"] in _wire_text(case.request(work_calls[0]))
        else:
            assert actual["delivery"] is None
    assert case.call_count(work_id, work_thread) == 1 and case.call_count(judge_id, judge_thread) == 4


@pytest.mark.asyncio
async def test_model_invalid_or_unrelated_responses_never_reuse_previous_valid_reply(source_case: RuntimeSourceCase) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    await case.plan(judge_id, [{"content": json.dumps({"run_id": "ptrun_previous", "definition_revision": 1,
        "decision": "proceed", "reason": "旧执行的有效判断", "result": {}})}])
    previous = await _send(case, judge_id, "生成另一执行的历史回复。")
    await _job(case, previous["job_id"])
    for index in range(5):
        task = await _create(case, _model_configuration(work_uri, judge_uri), f"invalid-{index}")
        run = await _begin(case, task, f"invalid-{index}")
        answer = _answer(run)
        if index == 0:
            content = "这不是JSON。"
        elif index == 1:
            answer["run_id"] = "ptrun_previous"
            content = json.dumps(answer)
        elif index == 2:
            answer["definition_revision"] += 1
            content = json.dumps(answer)
        elif index == 3:
            answer["decision"] = "maybe"
            content = json.dumps(answer)
        else:
            answer["reason"] = ""
            content = json.dumps(answer)
        await case.plan(judge_id, [{"content": content}])
        _release_match(case, f"invalid-{index}")
        actual = await _run(case, task, run, "failed")
        assert actual["decision"] is None and actual["delivery"] is None and actual["evaluation"] is not None
        assert "模型" in actual["error"]
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["lifecycle"] == "needs_attention"
    assert case.call_count(work_id, work_thread) == 0 and case.call_count(judge_id, judge_thread) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_busy_judge_serializes_checks_and_uses_only_final_model_output(source_case: RuntimeSourceCase) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    busy_call, = await case.plan(judge_id, [{"content": "先完成独立的已有工作。"}])
    case.control.arm(busy_call, "model.response_ready")
    busy = await _send(case, judge_id, "先占用判断Thread。")
    await asyncio.to_thread(case.control.acknowledged, busy_call, "model.response_ready", 30)
    run = await _begin(case, task, "queued-first")
    second = await _check(case, task, "queued-second")
    (case.workspace / "model-condition.txt").write_text("真正的检查材料", encoding="utf-8")
    intermediate = _tool_reply("read_file", {"path": "model-condition.txt"}, "check-read")
    intermediate["content"] = "这段中间说明不是最终判断。"
    _, final_call = await case.plan(judge_id, [intermediate, {"content": json.dumps(_answer(run, "wait"))}])
    _release_match(case, "queued-first")
    evaluation = await _evaluation(case, task, run)
    queued = (await case.client.get(f"/api/v1/jobs/{evaluation['job_id']}")).json()["data"]
    assert queued["status"] == "queued" and case.call_count(judge_id, judge_thread) == 1
    await case.container.persistent_task_runtime.stop()
    await case.container.persistent_task_runtime.start()
    case.control.arm_match("second-launch", "task.check.started_before_launch", {"task_id": task["task_id"]})
    case.control.release(busy_call, "model.response_ready")
    try:
        await _job(case, busy["job_id"])
        actual = await _run(case, task, run, "checked")
        await asyncio.to_thread(case.control.acknowledged_match, "second-launch")
        assert actual["decision"]["decision"] == "wait" and actual["evaluation"] == evaluation
        assert "真正的检查材料" in _wire_text(case.request(final_call))
        await case.plan(judge_id, [{"content": json.dumps(_answer(second, "wait"))}])
    finally:
        _release_match(case, "second-launch")
    following = await _run(case, task, second, "checked")
    assert following["evaluation"]["execution_id"] != evaluation["execution_id"]
    assert case.call_count(judge_id, judge_thread) == 4 and case.call_count(work_id, work_thread) == 0


@pytest.mark.asyncio
async def test_judge_isolation_and_late_dry_run_preserve_management_revision(source_case: RuntimeSourceCase) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    invalid = _model_configuration(work_uri, work_uri)
    response = await case.client.post(ROOT, json={"idempotency_key": "same-action", "configuration": invalid})
    assert response.status_code == 422, response.text
    invalid = _model_configuration(work_uri, judge_uri)
    invalid["trigger"].update(type_id="builtin.execution_end", source_uri=judge_uri)
    response = await case.client.post(ROOT, json={"idempotency_key": "same-source", "configuration": invalid})
    assert response.status_code == 422, response.text
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    for operation in ["pause", "edit", "trial"]:
        if operation == "trial":
            task = await _control(case, task, "pause")
        run = await _begin(case, task, operation, check_only=operation == "trial")
        call, = await case.plan(judge_id, [{"content": json.dumps(_answer(run, "complete" if operation == "trial" else "proceed"))}])
        case.control.arm(call, "model.response_ready")
        _release_match(case, operation)
        await asyncio.to_thread(case.control.acknowledged, call, "model.response_ready", 30)
        if operation == "edit":
            configuration = deepcopy(task["configuration"])
            configuration["context"]["content"] = "新的上下文不能替换已经冻结的检查。"
            response = await asyncio.wait_for(case.client.put(f"{ROOT}/{task['task_id']}", json={
                "expected_revision": task["revision"], "configuration": configuration,
            }), 1)
            assert response.status_code == 200, response.text
            changed = response.json()["data"]
        elif operation == "pause":
            changed = await asyncio.wait_for(_control(case, task, "pause"), 1)
        else:
            changed = task
        case.control.release(call, "model.response_ready")
        actual = await _run(case, task, run, "checked" if operation == "trial" else "suppressed")
        assert actual["decision"]["result"]["evaluated"] == operation and actual["delivery"] is None
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["revision"] == changed["revision"] and current["lifecycle"] == changed["lifecycle"]
        task = await _control(case, current, "resume") if operation != "trial" else current
    assert case.call_count(judge_id, judge_thread) == 3 and case.call_count(work_id, work_thread) == 0


@pytest.mark.asyncio
async def test_cancelled_model_check_is_failure_without_work_or_other_task_damage(source_case: RuntimeSourceCase) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    run = await _begin(case, task, "cancel-check")
    case.control.arm_match("cancel-runtime", "thread.runtime.acquired", {"session_id": judge_id})
    _release_match(case, "cancel-check")
    await asyncio.to_thread(case.control.acknowledged_match, "cancel-runtime")
    evaluation = await _evaluation(case, task, run)
    response = await case.client.post(f"/api/v1/jobs/{evaluation['job_id']}/control", json={"action": "cancel"})
    assert response.status_code == 200, response.text
    _release_match(case, "cancel-runtime")
    await _job(case, evaluation["job_id"], expected="cancelled")
    actual = await _run(case, task, run, "failed")
    assert actual["decision"] is None and actual["delivery"] is None and "未成功" in actual["error"]
    healthy = await _create(case, _configuration(work_uri), "independent-task")
    assert (await _run(case, healthy, await _check(case, healthy)))["delivery"]["state"] == "saved"
    assert case.call_count(judge_id, judge_thread) == case.call_count(work_id, work_thread) == 0


@pytest.mark.asyncio
async def test_model_acceptance_hard_restart_reuses_original_result_and_execution(restart_source_case: RestartRuntimeSourceCase) -> None:
    case = restart_source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    run = await _begin(case, task, "model-restart")
    judge_call, = await case.plan(judge_id, [{"content": json.dumps(_answer(run))}])
    work_call, = await case.plan(work_id, [{"content": "重启后处理原判断。"}])
    case.control.arm_match("accepted", "task.check.model_accepted_before_record", {"task_id": task["task_id"]})
    _release_match(case, "model-restart")
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "accepted")
    await _job(case, barrier["details"]["job_id"])
    assert "task-original-private" in _wire_text(case.request(judge_call))
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "accepted"))
    actual = await _run(case, task, run)
    assert actual["evaluation"]["execution_id"] == barrier["details"]["execution_id"]
    assert actual["evaluation"]["source_item_id"] == barrier["details"]["item_id"]
    assert actual["request_id"] == run["request_id"] and actual["definition_revision"] == run["definition_revision"]
    await _job(case, actual["delivery"]["job_id"])
    assert run["run_id"] in _wire_text(case.request(work_call))
    await case.restart(hard=True)
    assert await _run(case, task, run) == actual
    assert case.call_count(judge_id, judge_thread) == 1 and case.call_count(work_id, work_thread) == 1
