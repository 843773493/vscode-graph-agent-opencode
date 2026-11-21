"""通过真实任务、模型与恢复入口验证通用计量，不预填任务执行状态。"""

import asyncio
import json

import pytest

from tests.integration.backend.sessions.test_persistent_task_generations import (
    generation_configuration,
    generation_run,
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
    _create,
    _release_match,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 沿当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前文件隔离
)

USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


async def _usage(case, task_id: str, tokens: int) -> dict:
    async with asyncio.timeout(15):
        while True:
            response = await case.client.get(f"{ROOT}/{task_id}")
            assert response.status_code == 200, response.text
            state = response.json()["data"]
            if state["tokens_used"] == tokens:
                return state
            assert state["tokens_used"] < tokens, state
            await asyncio.sleep(0.02)


async def test_unlimited_tool_loop_usage_and_execution_identity_survive_restart(restart_source_case: RestartRuntimeSourceCase):
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target, "context_and_run"))
    assert task["configuration"]["token_budget"] is None
    await case.plan(session_id, [_tool_reply("get_goal", {}, "call_usage_goal"), {"content": "已完成本次任务"}], usage=USAGE)
    run = await _check(case, task, "first-run")
    accepted = await _run(case, task, run)
    await _job(case, accepted["delivery"]["job_id"])
    metered = await _usage(case, task["task_id"], 20)
    assert metered["revision"] == task["revision"] and metered["lifecycle"] == "active"
    assert metered["sync_version"] > task["sync_version"]
    await case.restart(hard=True)
    restored = await _usage(case, task["task_id"], 20)
    assert restored["revision"] == task["revision"]
    assert await _check(case, task, "first-run") == accepted
    assert case.call_count(session_id, thread_id) == 2
    # 产品会对空回复进行一次重试；两次真实调用都必须入账。
    await case.plan(session_id, [{"content": ""}, {"content": "空回复重试后完成"}], usage=USAGE)
    second = await _run(case, task, await _check(case, task, "second-run"))
    await _job(case, second["delivery"]["job_id"])
    final = await _usage(case, task["task_id"], 40)
    assert final["lifecycle"] == "active" and final["revision"] == task["revision"]
    assert second["delivery"]["job_id"] != accepted["delivery"]["job_id"]
    assert case.call_count(session_id, thread_id) == 4
    case.write_actual("task-usage-restart", {"before": metered, "restored": restored, "final": final})


async def test_model_check_spends_task_budget_and_suppresses_work(source_case: RuntimeSourceCase):
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    configuration = _model_configuration(work_uri, judge_uri)
    configuration["token_budget"] = 10
    task = await _create(case, configuration)
    run = await _begin(case, task, "judge-budget")
    await case.plan(judge_id, [{"content": json.dumps(_answer(run))}], usage=USAGE)
    _release_match(case, "judge-budget")
    actual = await _run(case, task, run, "suppressed")
    state = await _usage(case, task["task_id"], 10)
    assert state["lifecycle"] == "needs_attention" and "预算" in state["reason"]
    assert actual["evaluation"] is not None and actual["delivery"] is None
    assert case.call_count(judge_id, judge_thread) == 1
    assert case.call_count(work_id, work_thread) == 0
    response = await case.client.post(f"{ROOT}/{task['task_id']}/control", json={
        "expected_revision": state["revision"], "action": "resume",
    })
    assert response.status_code == 409 and "预算" in response.text


async def test_cancelled_execution_keeps_usage_from_the_completed_tool_call(source_case: RuntimeSourceCase):
    case = source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target, "context_and_run"))
    calls = await case.plan(session_id, [
        _tool_reply("get_goal", {}, "call_cancel_usage"), {"content": "中断后不应接受的回复"},
    ], usage=USAGE)
    case.control.arm(calls[1], "model.response_ready")
    run = await _run(case, task, await _check(case, task, "cancel-usage"))
    try:
        await asyncio.to_thread(case.control.acknowledged, calls[1], "model.response_ready", 20)
        response = await case.client.post(f"/api/v1/sessions/{session_id}/interrupt")
        assert response.status_code == 200, response.text
    finally:
        case.control.release(calls[1], "model.response_ready")
    await _job(case, run["delivery"]["job_id"], "cancelled")
    state = await _usage(case, task["task_id"], 10)
    assert state["lifecycle"] == "active" and case.call_count(session_id, thread_id) == 2


async def test_invalid_model_decision_still_accounts_the_completed_call(source_case: RuntimeSourceCase):
    case = source_case
    judge_id, _judge_thread, judge_uri = await _target(case)
    work_id, work_thread, work_uri = await _target(case)
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    run = await _begin(case, task, "invalid-judgment")
    await case.plan(judge_id, [{"content": "不是合法的判断 JSON"}], usage=USAGE)
    _release_match(case, "invalid-judgment")
    actual = await _run(case, task, run, "failed")
    state = await _usage(case, task["task_id"], 10)
    assert actual["delivery"] is None and state["lifecycle"] == "needs_attention"
    assert case.call_count(work_id, work_thread) == 0


@pytest.mark.parametrize("token_budget", [10, 20, None])
async def test_generation_report_obeys_budget_and_preserves_child_result(source_case: RuntimeSourceCase, token_budget: int | None):
    case = source_case
    session_id, thread_id, _uri = await _target(case)
    await case.plan(session_id, [{"content": "建立真实分支源"}])
    await _job(case, (await _send(case, session_id, "建立分支源上下文"))["job_id"])
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    configuration = generation_configuration(workspace_id, mode="fork_new_and_report_back", target={
        "workspace_id": workspace_id, "session_id": session_id, "thread_id": thread_id,
    })
    configuration["action"]["generation"]["session_strategy"]["report_back"] = "summary_and_link"
    configuration["action"]["generation"]["path_template"] = []
    configuration["token_budget"] = token_budget
    task = await _create(case, configuration)
    await case.plan(session_id, [{"content": "已收到分支结果"}], usage=USAGE)
    barrier = "budget-child"
    case.control.arm_match(barrier, "thread.runtime.acquired", {"execution_origin": "user_turn"})
    try:
        run = await _check(case, task)
        await asyncio.to_thread(case.control.acknowledged_match, barrier)
        accepted = await generation_run(case, task, run, accepted=True)
        child_id = accepted["generation"]["outputs"][0]["session_id"]
        await case.plan(child_id, [{"content": "已生成的分支结果必须保留"}], usage=USAGE)
    finally:
        _release_match(case, barrier)
    final = await _run(case, task, run, "suppressed" if token_budget == 10 else "delivered")
    assert final["generation"]["status"] == "completed"
    assert final["generation"]["outputs"][0]["session_id"] == child_id
    state = await _usage(case, task["task_id"], 10 if token_budget == 10 else 20)
    assert state["lifecycle"] == ("active" if token_budget is None else "needs_attention")
    if token_budget == 10:
        assert "未接受回报执行" in final["error"]
        assert final["generation"]["report_back_job_id"] is None
        assert case.call_count(session_id, thread_id) == 1
    else:
        assert final["generation"]["report_back_job_id"] is not None
        assert case.call_count(session_id, thread_id) == 2
