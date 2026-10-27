"""执行结束从产品生成的canonical终态增量触发，内存事件仅负责唤醒。"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.core.job_event_bus import EventType
from tests.integration.backend.sessions.test_persistent_task_notifications import _input
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
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)


def _event_configuration(source: str, target: str, *, execute: bool = False) -> dict:
    configuration = _configuration(target, "context_and_run" if execute else "context_only")
    configuration["trigger"].update(type_id="builtin.execution_end", source_uri=source)
    return configuration


async def _event_runs(case: ModelReplayCase, task: dict, count: int) -> list[dict]:
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
            assert response.status_code == 200, response.text
            runs = response.json()["data"]["items"]
            if len(runs) >= count and all(run["phase"] not in {"pending", "checking", "delivering"} for run in runs):
                assert len(runs) == count
                return runs
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_user_and_internal_execution_ends_share_original_identity_and_do_not_retrigger_from_action(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    source_id, source_thread, source_uri = await _target(case)
    target_id, target_thread, target_uri = await _target(case)
    await case.plan(source_id, [{"content": "建立订阅前的旧历史。"}])
    historical = await _send(case, source_id, "原有历史。")
    await _job(case, historical["job_id"])
    task = await _create(case, _event_configuration(source_uri, target_uri, execute=True))
    await case.plan(source_id, [{"content": "真正完成本次工作。"}])
    action_call, = await case.plan(target_id, [{"content": "响应工作执行结束。"}])
    accepted = await _send(case, source_id, "第一份实际工作。")
    await _job(case, accepted["job_id"])
    first, = await _event_runs(case, task, 1)
    await _job(case, first["delivery"]["job_id"])
    payload = first["trigger_payload"]
    assert payload["job_id"] == accepted["job_id"] and payload["outcome"] == "completed"
    assert payload["source_uri"] == source_uri and first["trigger_type"] == "execution_end"
    canonical = case.container.checkpointer.execution_completions(source_id, source_thread, after_commit_id=0)
    event, = [event for event in canonical.records if event.job_id == accepted["job_id"]]
    assert payload["execution_id"] == event.execution_id and payload["commit_id"] == event.commit_id
    assert first["request_id"] == event.request_id and event.request_id is not None
    assert _input(case, action_call, accepted["job_id"]) == [{"trigger_input": payload}]
    await case.container.job_event_bus.publish(accepted["job_id"], EventType.JOB_COMPLETED, {"session_id": source_id})
    assert await _event_runs(case, task, 1) == [first]
    assert (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"] == [first]

    internal = await _create(case, _configuration(source_uri, "context_and_run"), "internal-source")
    await case.plan(source_id, [{"content": "内部工作完成。"}])
    await case.plan(target_id, [{"content": "内部工作结束后的动作。"}])
    internal_run = await _run(case, internal, await _check(case, internal))
    await _job(case, internal_run["delivery"]["job_id"])
    first_again, second = await _event_runs(case, task, 2)
    assert first_again == first and second["trigger_payload"]["job_id"] == internal_run["delivery"]["job_id"]
    assert second["trigger_payload"]["execution_id"] == internal_run["delivery"]["execution_id"]
    await _job(case, second["delivery"]["job_id"])
    assert case.call_count(source_id, source_thread) == 3 and case.call_count(target_id, target_thread) == 2


@pytest.mark.asyncio
async def test_cancelled_execution_publishes_its_original_terminal_outcome(source_case: RuntimeSourceCase) -> None:
    case = source_case
    source_id, source_thread, source_uri = await _target(case)
    target_id, target_thread, target_uri = await _target(case)
    task = await _create(case, _event_configuration(source_uri, target_uri))
    case.control.arm_match("cancel-source", "thread.runtime.acquired", {"session_id": source_id})
    accepted = await _send(case, source_id, "取消这次真实工作。")
    await asyncio.to_thread(case.control.acknowledged_match, "cancel-source")
    response = await case.client.post(f"/api/v1/jobs/{accepted['job_id']}/control", json={"action": "cancel"})
    assert response.status_code == 200, response.text
    _release_match(case, "cancel-source")
    await _job(case, accepted["job_id"], expected="cancelled")
    run, = await _event_runs(case, task, 1)
    assert run["trigger_payload"]["job_id"] == accepted["job_id"] and run["trigger_payload"]["outcome"] == "cancelled"
    page = case.container.checkpointer.execution_completions(source_id, source_thread, after_commit_id=0)
    assert len(page.records) == 1 and page.records[0].outcome == "cancelled"
    assert case.container.checkpointer.execution_completions(source_id, source_thread,
        after_commit_id=run["trigger_payload"]["commit_id"]).records == ()
    assert run["delivery"]["state"] == "saved" and run["error"] is None
    assert case.call_count(source_id, source_thread) == case.call_count(target_id, target_thread) == 0


@pytest.mark.asyncio
async def test_execution_subscription_validation_pause_and_source_edit_preserve_explicit_boundaries(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    source_id, source_thread, source_uri = await _target(case)
    other_id, _other_thread, other_uri = await _target(case)
    target_id, target_thread, target_uri = await _target(case)
    configuration = _event_configuration(source_uri, target_uri)
    invalid = deepcopy(configuration)
    invalid["trigger"]["source_uri"] = None
    response = await case.client.post(ROOT, json={"idempotency_key": "missing-source", "configuration": invalid})
    assert response.status_code == 422, response.text
    missing_id = source_id[:-1] + ("0" if source_id[-1] != "0" else "1")
    invalid["trigger"]["source_uri"] = source_uri.replace(source_id, missing_id)
    response = await case.client.post(ROOT, json={"idempotency_key": "unknown-source", "configuration": invalid})
    assert response.status_code == 404, response.text
    task = await _create(case, configuration)
    task = await _control(case, task, "pause")
    await case.plan(source_id, [{"content": "暂停期间完成。"}])
    paused = await _send(case, source_id, "不应触发的工作。")
    await _job(case, paused["job_id"])
    task = await _control(case, task, "resume")
    assert await _event_runs(case, task, 0) == []
    configuration["name"] = "只改名称不能重置订阅"
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={"expected_revision": task["revision"], "configuration": configuration})
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    await case.plan(source_id, [{"content": "恢复后完成。"}])
    accepted = await _send(case, source_id, "需要触发的工作。")
    await _job(case, accepted["job_id"])
    first, = await _event_runs(case, task, 1)
    assert first["trigger_payload"]["job_id"] == accepted["job_id"] and first["definition_revision"] == task["definition_revision"]
    await case.plan(other_id, [{"content": "更换来源之前的旧历史。"}])
    old_other = await _send(case, other_id, "旧的其他来源。")
    await _job(case, old_other["job_id"])
    configuration["trigger"]["source_uri"] = other_uri
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={"expected_revision": task["revision"], "configuration": configuration})
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    await case.plan(other_id, [{"content": "更换来源后的工作完成。"}])
    latest = await _send(case, other_id, "新的其他来源。")
    await _job(case, latest["job_id"])
    first_again, second = await _event_runs(case, task, 2)
    assert first_again == first and second["trigger_payload"]["job_id"] == latest["job_id"]
    assert case.call_count(source_id, source_thread) == 2 and case.call_count(target_id, target_thread) == 0


@pytest.mark.asyncio
async def test_terminal_commit_before_task_acceptance_recovers_after_backend_hard_restart(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    source_id, _source_thread, source_uri = await _target(case)
    target_id, target_thread, target_uri = await _target(case)
    task = await _create(case, _event_configuration(source_uri, target_uri))
    case.control.arm_match("event-before-accept", "task.execution_source.read_before_accept", {"task_id": task["task_id"]})
    await case.plan(source_id, [{"content": "已持久完成但任务未接受。"}])
    accepted = await _send(case, source_id, "在窗口处重启。")
    await asyncio.to_thread(case.control.acknowledged_match, "event-before-accept")
    response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
    assert response.json()["data"]["items"] == []
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "event-before-accept"))
    original, = await _event_runs(case, task, 1)
    assert original["trigger_payload"]["job_id"] == accepted["job_id"] and original["error"] is None
    await case.restart(hard=True)
    assert await _event_runs(case, task, 1) == [original]
    call, = await case.plan(target_id, [{"content": "读取冷恢复后的结束事件。"}])
    consumed = await _send(case, target_id, "读取事件。")
    await _job(case, consumed["job_id"])
    assert _input(case, call, accepted["job_id"]) == [{"trigger_input": original["trigger_payload"]}]
    assert case.call_count(target_id, target_thread) == 1
