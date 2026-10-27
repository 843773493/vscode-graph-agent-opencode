"""外部通知复用真实任务账本、脚本与source投递，不建立旁路执行器。"""

from __future__ import annotations

import asyncio
import json
from xml.etree import ElementTree

import pytest

from tests.integration.backend.sessions.test_persistent_task_scripts import (
    _inputs,
    _script_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _configuration,
    _control,
    _create,
    _post,
    _release_match,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
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


async def _notify(case: RuntimeSourceCase | RestartRuntimeSourceCase, task: dict, key: str, payload: dict) -> dict:
    return await _post(case, f"{ROOT}/{task['task_id']}/notifications", {"idempotency_key": key, "payload": payload})


def _input(case: RuntimeSourceCase | RestartRuntimeSourceCase, call: str, marker: str) -> list[dict]:
    content, = [message["content"] for message in case.request(call)["messages"]
                if isinstance(message["content"], str) and marker in message["content"]]
    body = ElementTree.fromstring(content).findtext("untrusted_instructions")
    assert body is not None
    return [json.loads(line) for line in body.strip().splitlines()[1:]]


@pytest.mark.asyncio
async def test_external_notifications_keep_frozen_inputs_and_one_idempotent_delivery(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    program = """
payload = value['trigger']['payload']
print(json.dumps({'decision':'proceed' if payload['ready'] else 'wait','reason':'external-state','result':payload}))
"""
    configuration = _script_configuration(target, program, mode="context_and_run")
    configuration["trigger"]["type_id"] = "builtin.external"
    configuration["policies"]["match_mode"] = "rising_edge"
    task = await _create(case, configuration)
    endpoint = f"{ROOT}/{task['task_id']}/notifications"
    missing = await case.client.post(endpoint, json={"idempotency_key": "missing-payload"})
    assert missing.status_code == 422
    assert (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"] == []
    assert not (case.workspace / "checks.jsonl").exists() and case.call_count(session_id, thread_id) == 0
    waiting = {"ready": False, "event": "notification-original-wait"}
    wait = await _run(case, task, await _notify(case, task, "offline", waiting), "checked")
    assert wait["trigger_type"] == "external" and wait["trigger_payload"] == waiting and wait["scheduled_for"] is None
    assert wait["delivery"] is None and case.call_count(session_id, thread_id) == 0
    assert await _notify(case, task, "offline", waiting) == wait
    conflict = await case.client.post(endpoint, json={"idempotency_key": "offline", "payload": {**waiting, "ready": True}})
    assert conflict.status_code == 409, conflict.text
    ready = {"ready": True, "event": "notification-original-ready", "message": "服务已就绪"}
    call, = await case.plan(session_id, [{"content": "消费外部就绪通知。"}])
    duplicates = await asyncio.gather(*[_notify(case, task, "online", ready) for _ in range(8)])
    assert len({run["run_id"] for run in duplicates}) == 1
    actual = await _run(case, task, duplicates[0])
    await _job(case, actual["delivery"]["job_id"])
    assert actual["decision"]["result"] == ready
    assert _input(case, call, "notification-original-ready") == [
        {"trigger_input": ready}, {"condition_decision": actual["decision"]},
    ]
    repeated = await _run(case, task, await _notify(case, task, "still-online", ready), "checked")
    assert repeated["decision"]["decision"] == "proceed" and repeated["delivery"] is None
    assert case.call_count(session_id, thread_id) == 1
    inputs = _inputs(case.workspace)
    assert len(inputs) == 3 and inputs[1]["input"]["trigger"] == {
        "key": "online", "type": "external", "scheduled_for": None, "payload": ready,
    }
    configuration = task["configuration"]
    configuration["name"] = "编辑不改变已接受通知"
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={
        "expected_revision": task["revision"], "configuration": configuration,
    })
    assert response.status_code == 200, response.text
    task = await _control(case, response.json()["data"], "pause")
    assert await _notify(case, task, "online", ready) == actual
    refused = await case.client.post(endpoint, json={"idempotency_key": "while-paused", "payload": ready})
    assert refused.status_code == 409
    task = await _control(case, task, "resume")
    await _run(case, task, await _notify(case, task, "offline-again", waiting), "checked")
    call, = await case.plan(session_id, [{"content": "消费新边沿。"}])
    again = await _run(case, task, await _notify(case, task, "online-again", ready))
    await _job(case, again["delivery"]["job_id"])
    assert again["definition_revision"] == task["definition_revision"] == 2
    assert again["delivery"]["item_id"] != actual["delivery"]["item_id"]
    assert case.call_count(session_id, thread_id) == 2
    manual = await _create(case, _configuration(target), "manual")
    wrong_trigger = await case.client.post(f"{ROOT}/{manual['task_id']}/notifications", json={"idempotency_key": "wrong-trigger", "payload": ready})
    assert wrong_trigger.status_code == 400
    assert (await case.client.get(f"{ROOT}/{manual['task_id']}/runs")).json()["data"]["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["task.check.decided_before_delivery", "task.action.accepted_before_record"])
async def test_external_notification_recovers_original_input_and_acceptance(
    restart_source_case: RestartRuntimeSourceCase, phase: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["trigger"]["type_id"] = "builtin.external"
    task = await _create(case, configuration)
    payload = {"event": "notification-restart-original", "value": ["冻结输入", 17]}
    case.control.arm_match("notify-before-restart", phase, {"task_id": task["task_id"]})
    run = await _notify(case, task, "external-original", payload)
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "notify-before-restart")
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "notify-before-restart"))
    actual = await _run(case, task, run)
    assert actual["trigger_payload"] == payload and actual["request_id"] == run["request_id"]
    assert actual["definition_revision"] == run["definition_revision"]
    if phase == "task.action.accepted_before_record":
        assert actual["delivery"]["item_id"] == barrier["details"]["item_id"]
    assert await _notify(case, task, "external-original", payload) == actual
    conflict = await case.client.post(f"{ROOT}/{task['task_id']}/notifications", json={
        "idempotency_key": "external-original", "payload": {"event": "different-input"},
    })
    assert conflict.status_code == 409
    call, = await case.plan(session_id, [{"content": "读取原外部通知。"}])
    accepted = await _send(case, session_id, "读取已保存的通知。")
    await _job(case, accepted["job_id"])
    assert _input(case, call, "notification-restart-original") == [{"trigger_input": payload}]
    assert case.call_count(session_id, thread_id) == 1
