"""规则判断消费冻结输入，保留JSON身份、条件策略与纯计算恢复语义。"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.backend.sessions.test_persistent_task_notifications import (
    _input,
    _notify,
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


def _rule_configuration(target: str, pointer: str, expected: object, *, external: bool = True) -> dict:
    configuration = _configuration(target, mode="context_and_run")
    if external:
        configuration["trigger"]["type_id"] = "builtin.external"
    configuration["condition"] = {"type_id": "builtin.rule", "version": "1", "script": None,
                                  "rule": {"pointer": pointer, "expected": expected}}
    configuration["policies"]["match_mode"] = "first_match"
    return configuration


@pytest.mark.asyncio
async def test_rule_inputs_preserve_missing_null_boolean_and_structured_json_identity(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    cases = [
        ("/trigger/payload/status/0/a~1b", True, [None, False, 1, True]),
        ("/trigger/payload/value", {"good": True, "items": [1, 2]},
         [None, {"good": 1, "items": [1, 2]}, {"good": True, "items": [2, 1]}, {"items": [1.0, 2.0], "good": True}]),
        ("/trigger/payload/value", None, [None, None]),
    ]
    for case_index, (pointer, expected, values) in enumerate(cases):
        task = await _create(case, _rule_configuration(target, pointer, expected), f"rule-{case_index}")
        for index, value in enumerate(values):
            payload = {"marker": f"rule-observation-{case_index}-{index}"}
            if index:
                payload.update({"status": [{"a/b": value}]} if case_index == 0 else {"value": value})
            matched = index == len(values) - 1
            calls = await case.plan(session_id, [{"content": "已消费规则条件结果。"}]) if matched else []
            run = await _notify(case, task, f"case-{index}", payload)
            actual = await _run(case, task, run, "delivered" if matched else "checked")
            assert actual["decision"] == {
                "decision": "proceed" if matched else "wait",
                "reason": "规则匹配" if matched else ("规则未匹配" if index else "规则输入字段不存在"),
                "result": {"pointer": pointer, "found": bool(index), "actual": value, "expected": expected},
            }
            assert actual["error"] is None and bool(actual["delivery"]) == matched
            if matched:
                await _job(case, actual["delivery"]["job_id"])
                assert _input(case, calls[0], payload["marker"]) == [
                    {"trigger_input": payload}, {"condition_decision": actual["decision"]},
                ]
            assert case.call_count(session_id, thread_id) == case_index + int(matched)
        repeated = await _run(case, task, await _notify(case, task, "repeat-match", payload), "checked")
        assert repeated["decision"]["decision"] == "proceed" and repeated["delivery"] is None
    assert not (case.workspace / "checks.jsonl").exists()


@pytest.mark.asyncio
async def test_rule_validation_and_paused_dry_run_do_not_consume_first_match(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _rule_configuration(target, "/context/content", "ready", external=False)
    configuration["context"]["content"] = "ready"
    for pointer in ["invalid-pointer", "/~2"]:
        invalid = _rule_configuration(target, pointer, True)
        response = await case.client.post(ROOT, json={"idempotency_key": pointer, "configuration": invalid})
        assert response.status_code == 422, response.text
    invalid = _rule_configuration(target, "/context/content", None)
    del invalid["condition"]["rule"]["expected"]
    response = await case.client.post(ROOT, json={"idempotency_key": "missing-expected", "configuration": invalid})
    assert response.status_code == 422, response.text
    task = await _create(case, configuration)
    task = await _control(case, task, "pause")
    trial = await _run(case, task, await _check(case, task, "trial", check_only=True), "checked")
    assert trial["decision"]["decision"] == "proceed" and trial["delivery"] is None
    current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
    assert current["revision"] == task["revision"] and current["lifecycle"] == "paused"
    assert case.call_count(session_id, thread_id) == 0
    task = await _control(case, task, "resume")
    call, = await case.plan(session_id, [{"content": "正式规则检查通过。"}])
    actual = await _run(case, task, await _check(case, task, "live"))
    await _job(case, actual["delivery"]["job_id"])
    assert case.request(call) and case.call_count(session_id, thread_id) == 1


@pytest.mark.asyncio
async def test_checking_rule_recovers_from_its_frozen_input_without_unknown_script_result(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    configuration = _rule_configuration(target, "/trigger/payload/ready", True)
    configuration["action"]["delivery_mode"] = "context_only"
    task = await _create(case, configuration)
    case.control.arm_match("rule-before-restart", "task.check.started_before_launch", {"task_id": task["task_id"]})
    payload = {"ready": True, "marker": "rule-original-input"}
    run = await _notify(case, task, "original", payload)
    await asyncio.to_thread(case.control.acknowledged_match, "rule-before-restart")
    newer = await _notify(case, task, "newer", {"ready": False, "marker": "rule-newer-input"})
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "rule-before-restart"))
    actual = await _run(case, task, run)
    assert actual["error"] is None and actual["trigger_payload"] == payload
    assert actual["decision"]["result"]["actual"] is True and actual["request_id"] == run["request_id"]
    waiting = await _run(case, task, newer, "checked")
    assert waiting["decision"]["result"]["actual"] is False and waiting["delivery"] is None
    call, = await case.plan(session_id, [{"content": "读取重启后保存的规则结果。"}])
    accepted = await _send(case, session_id, "读取规则事件。")
    await _job(case, accepted["job_id"])
    assert _input(case, call, "rule-original-input") == [
        {"trigger_input": payload}, {"condition_decision": actual["decision"]},
    ]
    assert case.call_count(session_id, thread_id) == 1
