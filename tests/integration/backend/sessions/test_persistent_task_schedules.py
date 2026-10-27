"""经任务产品入口验证计划游标、自动触发、真实工作执行及冷恢复。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.backend.sessions.test_persistent_task_notifications import (
    _input,
)
from tests.integration.backend.sessions.test_persistent_task_scripts import (
    _inputs,
    _script_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
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
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)


@pytest.fixture
def task_clock(source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch):
    current = [datetime(2026, 10, 27, tzinfo=UTC)]
    runtime = source_case.container.persistent_task_runtime
    monkeypatch.setattr(runtime, "clock", lambda: current[0])

    def advance(seconds: int) -> None:
        current[0] += timedelta(seconds=seconds)
        runtime._wake.set()

    return current, advance


async def _history(case, task: dict, count: int) -> list[dict]:
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
            assert response.status_code == 200, response.text
            runs = response.json()["data"]["items"]
            assert len(runs) <= count, runs
            if len(runs) == count and all(row["phase"] not in {"pending", "checking", "delivering"} for row in runs):
                return runs
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_each_interval_uses_the_same_execution_chain_and_preserves_cursor_on_edit(
    source_case: RuntimeSourceCase, task_clock,
) -> None:
    case = source_case
    clock, advance = task_clock
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target, "context_and_run")
    configuration["trigger"] = {"type_id": "builtin.interval", "version": "1", "interval_seconds": 60}
    configuration["policies"]["misfire"] = "catch_up"
    for trigger in (
        {"type_id": "builtin.interval", "version": "1"},
        {"type_id": "builtin.cron", "version": "1", "expression": "invalid"},
        {"type_id": "builtin.cron", "version": "1", "expression": "0 9 * * *", "timezone": "Unknown/Nowhere"},
    ):
        invalid = {**configuration, "trigger": trigger}
        response = await case.client.post(ROOT, json={"idempotency_key": "invalid-time", "configuration": invalid})
        assert response.status_code == 422, response.text
    task = await _create(case, configuration)
    task_id = task["task_id"]
    anchor = clock[0]
    assert datetime.fromisoformat(task["next_run_at"]) == anchor + timedelta(seconds=60)
    calls = await case.plan(session_id, [{"content": f"计划工作{i}完成。"} for i in range(3)])
    for index in range(3):
        advance(60)
        runs = await _history(case, task, index + 1)
        actual = next(run for run in runs if datetime.fromisoformat(run["scheduled_for"]) == clock[0])
        assert actual["trigger_type"] == "interval" and not actual["check_only"]
        assert actual["decision"]["decision"] == "proceed"
        await _job(case, actual["delivery"]["job_id"])
        assert _wire_text(case.request(calls[index])["messages"]).count("task-original-private") == index + 1
        if index == 0:
            configuration = deepcopy(task["configuration"])
            configuration["name"] = "编辑名称仍沿原计划"
            response = await case.client.put(f"{ROOT}/{task_id}", json={"expected_revision": task["revision"], "configuration": configuration})
            assert response.status_code == 200, response.text
            updated = response.json()["data"]
            assert updated["task_id"] == task_id and updated["definition_revision"] == 2
            assert datetime.fromisoformat(updated["next_run_at"]) == anchor + timedelta(seconds=120)
            task = updated
    assert case.call_count(session_id, thread_id) == 3
    assert case.sql(session_id, thread_id, "SELECT execution_origin,turn_id FROM executions") == [("internal", None)] * 3
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
    assert [run["definition_revision"] for run in runs] == [1, 2, 2]
    case.write_actual("interval-execution", {"task": task, "runs": runs, "requests": case.requests()})


@pytest.mark.asyncio
async def test_enabling_and_disabling_a_condition_preserves_the_task_schedule_and_history(
    source_case: RuntimeSourceCase, task_clock,
) -> None:
    case = source_case
    clock, advance = task_clock
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target, "context_and_run")
    configuration["trigger"] = {"type_id": "builtin.interval", "version": "1", "interval_seconds": 60}
    task = await _create(case, configuration)
    task_id, anchor = task["task_id"], clock[0]
    calls = await case.plan(session_id, [{"content": f"按同一计划执行第{index}次工作。"} for index in range(4)])

    for count in (1, 2):
        advance(60)
        initial = await _history(case, task, count)
        latest = initial[-1]
        assert latest["decision"] == {"decision": "proceed", "reason": "无附加条件，始终通过", "result": {}}
        await _job(case, latest["delivery"]["job_id"])
        assert case.call_count(session_id, thread_id) == count
    assert not (case.workspace / "checks.jsonl").exists()

    # 在两个计划点之间开启真实脚本，不能重置计划或新建任务。
    advance(10)
    program = """
ready = pathlib.Path('ready.txt').read_text(encoding='utf-8') == 'ready'
print(json.dumps({'decision':'proceed' if ready else 'wait','reason':'scheduled-condition',
                  'result':{'ready':ready,'scheduled_for':value['trigger']['scheduled_for']}}))
"""
    configuration["condition"] = _script_configuration(target, program)["condition"]
    response = await case.client.put(f"{ROOT}/{task_id}", json={
        "expected_revision": task["revision"], "configuration": configuration,
    })
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    assert task["task_id"] == task_id and task["definition_revision"] == 2
    assert datetime.fromisoformat(task["next_run_at"]) == anchor + timedelta(seconds=180)
    assert (await case.client.get(f"{ROOT}/{task_id}/runs")).json()["data"]["items"] == initial

    signal = case.workspace / "ready.txt"
    signal.write_text("waiting", encoding="utf-8")
    advance(50)
    waiting = await _history(case, task, 3)
    assert waiting[:2] == initial
    assert waiting[-1]["decision"] == {"decision": "wait", "reason": "scheduled-condition", "result": {
        "ready": False, "scheduled_for": clock[0].isoformat(),
    }}
    assert waiting[-1]["phase"] == "checked" and waiting[-1]["delivery"] is None
    assert case.call_count(session_id, thread_id) == 2

    signal.write_text("ready", encoding="utf-8")
    advance(60)
    matched = await _history(case, task, 4)
    assert matched[:3] == waiting
    assert matched[-1]["decision"]["result"] == {"ready": True, "scheduled_for": clock[0].isoformat()}
    await _job(case, matched[-1]["delivery"]["job_id"])
    assert case.call_count(session_id, thread_id) == 3
    actual_inputs = _inputs(case.workspace)
    assert [entry["input"]["trigger"]["scheduled_for"] for entry in actual_inputs] == [
        (anchor + timedelta(seconds=offset)).isoformat() for offset in (180, 240)
    ]
    assert {entry["input"]["task_id"] for entry in actual_inputs} == {task_id}
    assert [entry["input"]["definition_revision"] for entry in actual_inputs] == [2, 2]
    assert _input(case, calls[2], "scheduled-condition") == [{"condition_decision": matched[-1]["decision"]}]

    configuration["condition"] = _configuration(target)["condition"]
    response = await case.client.put(f"{ROOT}/{task_id}", json={
        "expected_revision": task["revision"], "configuration": configuration,
    })
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    assert task["task_id"] == task_id and task["definition_revision"] == 3
    assert datetime.fromisoformat(task["next_run_at"]) == anchor + timedelta(seconds=300)
    advance(60)
    final = await _history(case, task, 5)
    await _job(case, final[-1]["delivery"]["job_id"])
    assert final[:4] == matched
    assert [row["definition_revision"] for row in final] == [1, 1, 2, 2, 3]
    assert [datetime.fromisoformat(row["scheduled_for"]) for row in final] == [
        anchor + timedelta(seconds=60 * index) for index in range(1, 6)
    ]
    assert len({row["run_id"] for row in final}) == 5
    assert final[-1]["decision"] == {"decision": "proceed", "reason": "无附加条件，始终通过", "result": {}}
    assert _inputs(case.workspace) == actual_inputs
    assert case.call_count(session_id, thread_id) == len(case.requests()) == 4
    assert len((await case.client.get(ROOT)).json()["data"]["items"]) == 1
    assert case.sql(session_id, thread_id, "SELECT execution_origin,turn_id FROM executions") == [("internal", None)] * 4
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
    case.write_actual("schedule-condition-edit", {"task": task, "runs": final, "checks": actual_inputs, "requests": case.requests()})


@pytest.mark.asyncio
@pytest.mark.parametrize("misfire,offsets", [("skip", []), ("run_latest", [180]), ("catch_up", [60, 120, 180])])
async def test_downtime_recovery_uses_durable_cursor_and_explicit_misfire_policy(
    source_case: RuntimeSourceCase, task_clock, misfire: str, offsets: list[int],
) -> None:
    case = source_case
    clock, advance = task_clock
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["trigger"] = {"type_id": "builtin.interval", "version": "1", "interval_seconds": 60}
    configuration["policies"]["misfire"] = misfire
    task = await _create(case, configuration)
    anchor = clock[0]
    runtime = case.container.persistent_task_runtime
    await runtime.stop()
    advance(185)
    await runtime.start()
    runs = await _history(case, task, len(offsets) or 1)
    delivered = [row for row in runs if row["phase"] == "delivered"]
    assert [datetime.fromisoformat(row["scheduled_for"]) for row in delivered] == [anchor + timedelta(seconds=n) for n in offsets]
    if misfire == "skip":
        assert runs[0]["phase"] == "skipped" and runs[0]["delivery"] is None
        assert datetime.fromisoformat(runs[0]["scheduled_for"]) == anchor + timedelta(seconds=180)
    response = await case.client.get(f"{ROOT}/{task['task_id']}")
    assert datetime.fromisoformat(response.json()["data"]["next_run_at"]) == anchor + timedelta(seconds=240)
    await runtime.stop()
    await runtime.start()
    # 下一有效点再次执行，证明重启没有重投旧计划或重置游标。
    advance(55)
    final = await _history(case, task, len(runs) + 1)
    assert final[:len(runs)] == runs
    assert datetime.fromisoformat(final[-1]["scheduled_for"]) == anchor + timedelta(seconds=240)
    assert final[-1]["phase"] == "delivered"
    assert await case.container.job_service.list(session_id=session_id) == []
    assert case.call_count(session_id, thread_id) == 0 and case.requests() == []
    case.write_actual("misfire-recovery", {"misfire": misfire, "runs": final})


@pytest.mark.asyncio
async def test_cron_timezone_and_paused_schedule_resume_without_reanchoring(
    source_case: RuntimeSourceCase, task_clock,
) -> None:
    case = source_case
    clock, advance = task_clock
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["trigger"] = {"type_id": "builtin.cron", "version": "1", "expression": "0 9 * * *", "timezone": "Asia/Shanghai"}
    configuration["policies"]["misfire"] = "run_latest"
    task = await _create(case, configuration)
    assert datetime.fromisoformat(task["next_run_at"]) == clock[0] + timedelta(hours=1)
    paused = await _control(case, task, "pause")
    advance(2 * 86400 + 3600)
    assert (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"] == []
    resumed = await _control(case, paused, "resume")
    runs = await _history(case, resumed, 1)
    assert datetime.fromisoformat(runs[0]["scheduled_for"]) == clock[0]
    assert runs[0]["trigger_type"] == "cron" and runs[0]["phase"] == "delivered"
    assert case.call_count(session_id, thread_id) == 0
    assert await case.container.job_service.list(session_id=session_id) == []
    case.write_actual("cron-resume", {"task": resumed, "runs": runs})


@pytest.mark.asyncio
async def test_due_trigger_is_persisted_during_recovery_and_pause_suppresses_delivery(
    source_case: RuntimeSourceCase, task_clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    _clock, advance = task_clock
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["trigger"] = {"type_id": "builtin.interval", "version": "1", "interval_seconds": 60}
    task = await _create(case, configuration)
    runtime = case.container.persistent_task_runtime
    await runtime.stop()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime._process_task

    async def recovering(task_id: str) -> None:
        entered.set()
        await release.wait()
        await original(task_id)

    monkeypatch.setattr(runtime, "_process_task", recovering)
    try:
        await asyncio.wait_for(runtime.start(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        advance(60)
        async with asyncio.timeout(5):
            while True:
                rows = (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"]
                if rows:
                    break
                await asyncio.sleep(0.02)
        pending, = rows
        assert pending["phase"] == "pending"
        current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
        assert current["recovering"] is True
        await _control(case, task, "pause")
    finally:
        release.set()
    actual = await _run(case, task, pending, "suppressed")
    assert actual["delivery"] is None and case.call_count(session_id, thread_id) == 0
    assert await case.container.job_service.list(session_id=session_id) == []
    case.write_actual("scheduled-during-recovery", {"pending": pending, "final": actual})


@pytest.mark.asyncio
async def test_real_process_restart_reuses_committed_schedule_trigger(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    _session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["trigger"] = {"type_id": "builtin.interval", "version": "1", "interval_seconds": 2}
    configuration["policies"]["misfire"] = "run_latest"
    task = await _create(case, configuration)
    case.control.arm_match("scheduled-loaded", "task.run.loaded", {"task_id": task["task_id"]})
    case.control.advance_wait(2)
    await asyncio.to_thread(case.control.acknowledged_match, "scheduled-loaded")
    before = (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"]
    original = before[0]
    assert original["phase"] == "pending" and original["scheduled_for"] is not None
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "scheduled-loaded"))
    actual = await _run(case, task, original)
    await _control(case, task, "pause")
    after = (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"]
    assert actual["run_id"] == original["run_id"] and actual["trigger_key"] == original["trigger_key"]
    assert actual["scheduled_for"] == original["scheduled_for"] and actual["request_id"] == original["request_id"]
    assert len({row["scheduled_for"] for row in after}) == len(after)
    assert case.call_count(_session_id, thread_id) == 0
    case.write_actual("scheduled-process-restart", {"before": before, "after": after, "original": actual})
