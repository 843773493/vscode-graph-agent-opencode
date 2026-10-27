"""条件策略经真实任务检查、模型请求和Backend重启保持同一进度。"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.backend.sessions.test_persistent_task_scripts import (
    _script_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _check,
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
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)

PROGRAM = """
decision = pathlib.Path('signal.txt').read_text(encoding='utf-8')
print(json.dumps({'decision':decision,'reason':'service-state','result':{'observation':'observed:'+value['trigger']['key']}}))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [
    ("first_match", [False, True, False, False, False, False]),
    ("rising_edge", [False, True, False, False, False, True]),
    ("each_match", [False, True, True, True, False, True]),
])
async def test_condition_policies_distinguish_repeated_readiness_and_new_edges(
    source_case: RuntimeSourceCase, mode: str, expected: list[bool],
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _script_configuration(target, PROGRAM, mode="context_and_run")
    configuration["policies"]["match_mode"] = mode
    task = await _create(case, configuration)
    signal = case.workspace / "signal.txt"
    deliveries = []
    for index, (decision, deliver) in enumerate(zip(["wait", "proceed", "proceed", "proceed", "wait", "proceed"], expected, strict=True)):
        # 试运行不能抢占首次命中，也不能用wait重置正式边沿。
        if index in {1, 2, 5}:
            signal.write_text("wait" if index == 2 else "proceed", encoding="utf-8")
            trial = await _check(case, task, f"trial-{index}", check_only=True)
            assert (await _run(case, task, trial, "checked"))["delivery"] is None
        if index == 3:
            task = await _control(case, task, "pause")
            task = await _control(case, task, "resume")
            configuration = task["configuration"]
            configuration["name"] = "同一任务改名不重置条件事实"
            response = await case.client.put(f"{ROOT}/{task['task_id']}", json={
                "expected_revision": task["revision"], "configuration": configuration,
            })
            assert response.status_code == 200, response.text
            task = response.json()["data"]
        signal.write_text(decision, encoding="utf-8")
        calls = await case.plan(session_id, [{"content": f"处理第{index}次就绪。"}]) if deliver else []
        run = await _check(case, task, f"live-{index}")
        actual = await _run(case, task, run, "delivered" if deliver else "checked")
        assert actual["decision"] == {"decision": decision, "reason": "service-state", "result": {"observation": f"observed:live-{index}"}}
        assert actual["error"] is None and bool(actual["delivery"]) == deliver
        assert await _check(case, task, f"live-{index}") == actual
        if deliver:
            deliveries.append(actual["delivery"]["item_id"])
            await _job(case, actual["delivery"]["job_id"])
            assert f"observed:live-{index}" in _wire_text(case.request(calls[0])["messages"])
        assert case.call_count(session_id, thread_id) == sum(expected[:index + 1])
    assert len(set(deliveries)) == len(deliveries) == sum(expected)
    assert (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]["lifecycle"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,phase", [
    ("first_match", "task.action.accepted_before_record"),
    ("rising_edge", "task.check.decided_before_delivery"),
])
async def test_match_facts_and_original_delivery_survive_hard_restart(
    restart_source_case: RestartRuntimeSourceCase, mode: str, phase: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    configuration = _script_configuration(target, PROGRAM)
    configuration["policies"]["match_mode"] = mode
    task = await _create(case, configuration)
    signal = case.workspace / "signal.txt"
    signal.write_text("proceed", encoding="utf-8")
    case.control.arm_match("match-before-restart", phase, {"task_id": task["task_id"]})
    run = await _check(case, task, "match-original")
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "match-before-restart")
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "match-before-restart"))
    actual = await _run(case, task, run)
    assert actual["request_id"] == run["request_id"] and actual["definition_revision"] == run["definition_revision"]
    if phase == "task.action.accepted_before_record":
        assert actual["delivery"]["item_id"] == barrier["details"]["item_id"]
    repeat = await _run(case, task, await _check(case, task, "match-repeated"), "checked")
    assert repeat["decision"]["decision"] == "proceed" and repeat["delivery"] is None
    signal.write_text("wait", encoding="utf-8")
    await _run(case, task, await _check(case, task, "offline"), "checked")
    signal.write_text("proceed", encoding="utf-8")
    again = await _run(case, task, await _check(case, task, "match-again"), "delivered" if mode == "rising_edge" else "checked")
    assert bool(again["delivery"]) == (mode == "rising_edge")
    call, = await case.plan(session_id, [{"content": "核对重启后保留的条件事件。"}])
    accepted = await _send(case, session_id, "读取已保存的就绪事件。")
    await _job(case, accepted["job_id"])
    wire = _wire_text(case.request(call)["messages"])
    assert wire.count("observed:match-original") == 1 and "observed:match-repeated" not in wire
    assert wire.count("observed:match-again") == (1 if mode == "rising_edge" else 0)
    assert case.call_count(session_id, thread_id) == 1
