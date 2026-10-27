"""持久任务经公开 API 形成状态，再核对真实 source、模型请求及进程恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Literal

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import thread_resource_uri
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
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import control_request

ROOT = "/api/v1/persistent-tasks"


def _release_match(case: ModelReplayCase, barrier_id: str) -> None:
    control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
        "action": "release_match", "barrier_id": barrier_id,
    })


def _configuration(target: str, mode: Literal["context_only", "context_and_run"] = "context_only") -> dict:
    return {
        "name": "服务就绪", "trigger": {"type_id": "builtin.manual", "version": "1", "interval_seconds": None,
                                      "expression": None, "timezone": "UTC", "source_uri": None},
        "condition": {"type_id": "builtin.always", "version": "1", "script": None, "rule": None, "model": None},
        "context": {"type_id": "builtin.literal", "version": "1", "mode": "event", "content": "task-original-private：服务已经就绪。"},
        "action": {"type_id": "builtin.runtime_source", "version": "1", "target_uri": target,
                   "delivery_mode": mode, "delivery_policy": "after_turn"},
        "policies": {"completion": "continuous", "misfire": "skip", "match_mode": "each_match"},
    }


async def _post(case: ModelReplayCase, path: str, payload: dict) -> dict:
    response = await case.client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    if path.endswith("/checks") and response.json()["data"]["phase"] == "pending":
        assert response.json()["data"]["request_id"] == response.json()["request_id"]
    return response.json()["data"]


async def _target(case: ModelReplayCase) -> tuple[str, str, str]:
    session_id = await create_session(case.client, "持久任务目标")
    response = await case.client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert response.status_code == 200, response.text
    thread_id = response.json()["data"]["thread_id"]
    response = await case.client.get("/api/v1/workspace")
    assert response.status_code == 200, response.text
    workspace_id = response.json()["data"]["workspace_id"]
    return session_id, thread_id, thread_resource_uri(workspace_id, session_id, thread_id)


async def _create(case: ModelReplayCase, configuration: dict, key: str = "create") -> dict:
    return await _post(case, ROOT, {"idempotency_key": key, "configuration": configuration, "enabled": True})


async def _check(case: ModelReplayCase, task: dict, key: str = "check", *, check_only: bool = False) -> dict:
    return await _post(case, f"{ROOT}/{task['task_id']}/checks", {
        "expected_revision": task["revision"], "idempotency_key": key, "check_only": check_only,
    })


async def _run(case: ModelReplayCase, task: dict, run: dict, phase: str = "delivered") -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
            assert response.status_code == 200, response.text
            actual, = [row for row in response.json()["data"]["items"] if row["run_id"] == run["run_id"]]
            if actual["phase"] not in {"pending", "checking", "delivering"}:
                assert actual["phase"] == phase, actual
                return actual
            await asyncio.sleep(0.02)


async def _control(case: ModelReplayCase, task: dict, action: str) -> dict:
    return await _post(case, f"{ROOT}/{task['task_id']}/control", {
        "expected_revision": task["revision"], "action": action,
    })


@pytest.mark.asyncio
async def test_component_validation_revision_and_real_dry_run(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    invalid = []
    missing = deepcopy(configuration)
    del missing["condition"]
    invalid.append(missing)
    for key, field, value in (("condition", "version", "99"), ("condition", "type_id", "unknown"),
                              ("condition", "type_id", "builtin.script"),
                              ("trigger", "type_id", "builtin.cron")):
        candidate = deepcopy(configuration)
        candidate[key][field] = value
        invalid.append(candidate)
    for candidate in invalid:
        response = await case.client.post(ROOT, json={"idempotency_key": "invalid", "configuration": candidate})
        assert response.status_code == 422, response.text
    assert (await case.client.get(ROOT)).json()["data"]["items"] == []
    task = await _create(case, configuration)
    assert await _create(case, configuration) == task
    conflict = deepcopy(configuration)
    conflict["name"] = "不同创建内容"
    response = await case.client.post(ROOT, json={"idempotency_key": "create", "configuration": conflict})
    assert response.status_code == 409, response.text
    run = await _check(case, task, check_only=True)
    actual = await _run(case, task, run, "checked")
    assert actual["decision"] == {"decision": "proceed", "reason": "无附加条件，始终通过", "result": {}}
    assert actual["delivery"] is None and actual["error"] is None
    assert await _check(case, task, check_only=True) == actual
    response = await case.client.post(f"{ROOT}/{task['task_id']}/checks", json={
        "idempotency_key": "check", "expected_revision": task["revision"], "check_only": False,
    })
    assert response.status_code == 409, response.text
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={
        "expected_revision": task["revision"], "configuration": conflict,
    })
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert (updated["revision"], updated["definition_revision"]) == (2, 2)
    assert updated["configuration"] == conflict
    assert (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"] == [actual]
    response = await case.client.post(f"{ROOT}/{task['task_id']}/control", json={"expected_revision": 1, "action": "pause"})
    assert response.status_code == 409, response.text
    paused = await _control(case, updated, "pause")
    assert (paused["revision"], paused["definition_revision"], paused["lifecycle"]) == (3, 2, "paused")
    response = await case.client.post(f"{ROOT}/{task['task_id']}/checks", json={"expected_revision": 3, "idempotency_key": "paused"})
    assert response.status_code == 409, response.text
    trial = await _check(case, paused, "paused-trial", check_only=True)
    await _run(case, paused, trial, "checked")
    resumed = await _control(case, paused, "resume")
    terminated = await _control(case, resumed, "terminate")
    assert (terminated["lifecycle"], terminated["revision"], terminated["definition_revision"]) == ("terminated", 5, 2)
    assert await case.container.job_service.list(session_id=session_id) == []
    assert case.call_count(session_id, thread_id) == 0 and case.requests() == []
    assert (await case.client.get(ROOT + "/missing")).status_code == 404


@pytest.mark.asyncio
async def test_context_only_task_is_saved_once_and_real_user_consumes_it(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["policies"]["completion"] = "after_delivery"
    task = await _create(case, configuration)
    run = await _check(case, task)
    actual = await _run(case, task, run)
    assert actual["delivery"]["state"] == "saved"
    assert actual["delivery"]["job_id"] is None and actual["delivery"]["execution_id"] is None
    assert await _check(case, task) == actual
    assert await case.container.job_service.list(session_id=session_id) == []
    assert (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]["lifecycle"] == "complete"
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions") == [(0,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
    call, = await case.plan(session_id, [{"content": "已读取任务状态。"}])
    accepted = await _send(case, session_id, "读取当前服务状态。")
    await _job(case, accepted["job_id"])
    assert _wire_text(case.request(call)["messages"]).count("task-original-private") == 1
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(1,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM item_catalog WHERE item_id=?",
                    (actual["delivery"]["item_id"],)) == [(1,)]
    case.write_actual("context-only-task", {"task": task, "run": actual, "request": case.request(call)})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("context_mode", ["event", "state"])
@pytest.mark.parametrize("control", ["pause", "terminate"])
async def test_busy_task_queues_without_fake_user_turn_and_preserves_accepted_work_after_control(
    source_case: RuntimeSourceCase, context_mode: str, control: str,
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target, "context_and_run")
    configuration["context"]["mode"] = context_mode
    task = await _create(case, configuration)
    assert task["configuration"]["action"]["target_uri"].startswith(f"boxteam://{case.gateway_id}/")
    first_call, task_call = await case.plan(session_id, [{"content": "用户工作完成。"}, {"content": "持久任务工作完成。"}])
    case.control.arm(first_call, "model.response_ready")
    case.control.arm_match("task-accepted", "task.action.accepted_before_record", {"task_id": task["task_id"]})
    user = await _send(case, session_id, "先处理用户工作。")
    try:
        await asyncio.to_thread(case.control.acknowledged, first_call, "model.response_ready", 30)
        run = await _check(case, task)
        barrier = await asyncio.to_thread(case.control.acknowledged_match, "task-accepted")
        changed = await _control(case, task, control)
        expected_lifecycle = "paused" if control == "pause" else "terminated"
        assert changed["lifecycle"] == expected_lifecycle
        response = await case.client.get(f"/api/v1/jobs/{barrier['details']['job_id']}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "queued"
        assert case.call_count(session_id, thread_id) == 1
    finally:
        _release_match(case, "task-accepted")
        case.control.release(first_call, "model.response_ready")
    actual = await _run(case, task, run)
    delivery = actual["delivery"]
    assert delivery["state"] == "accepted"
    assert delivery["job_id"] == barrier["details"]["job_id"]
    await _job(case, user["job_id"])
    await _job(case, delivery["job_id"])
    assert _wire_text(case.request(task_call)["messages"]).count("task-original-private") == 1
    assert case.sql(session_id, thread_id, "SELECT execution_origin,turn_id FROM executions ORDER BY created_at") == [
        ("user_turn", user["turn_id"]), ("internal", None),
    ]
    notice_id, = case.sql(session_id, thread_id,
        "SELECT item_id FROM item_catalog WHERE execution_id=? AND semantic_kind='runtime_notice'", (delivery["execution_id"],))[0]
    assert (notice_id == delivery["item_id"]) == (context_mode == "event")
    assert case.sql(session_id, thread_id, "SELECT execution_id,job_id,source_item_id,gateway_id FROM runtime_execution_wakeups") == [
        (delivery["execution_id"], delivery["job_id"], notice_id, case.gateway_id),
    ]
    assert (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]["lifecycle"] == expected_lifecycle
    case.write_actual("busy-task", {"run": actual, "control": changed, "request": case.request(task_call)})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "edit", "terminate"])
async def test_management_revision_suppresses_unaccepted_action(source_case: RuntimeSourceCase, operation: str) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target))
    case.control.arm_match("task-loaded", "task.run.loaded", {"task_id": task["task_id"]})
    run = await _check(case, task)
    try:
        await asyncio.to_thread(case.control.acknowledged_match, "task-loaded")
        if operation in {"pause", "terminate"}:
            changed = await _control(case, task, operation)
        else:
            configuration = deepcopy(task["configuration"])
            configuration["context"]["content"] = "task-updated-private：新的定义。"
            response = await case.client.put(f"{ROOT}/{task['task_id']}", json={"expected_revision": 1, "configuration": configuration})
            assert response.status_code == 200, response.text
            changed = response.json()["data"]
    finally:
        _release_match(case, "task-loaded")
    actual = await _run(case, task, run, "suppressed")
    assert actual["delivery"] is None
    assert actual["task_revision"] == actual["definition_revision"] == 1
    assert changed["revision"] == 2
    assert await case.container.job_service.list(session_id=session_id) == []
    assert case.call_count(session_id, thread_id) == 0


def _sql(workspace: Path, session_id: str, thread_id: str, statement: str) -> list[tuple]:
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    database = resolver.resolve_thread_node(session_id, thread_id) / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(statement).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_only", "context_and_run"])
async def test_real_backend_restart_reconciles_accepted_action_before_revision_suppression(
    restart_source_case: RestartRuntimeSourceCase, mode: Literal["context_only", "context_and_run"],
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target, mode))
    if mode == "context_and_run":
        call, = await case.plan(session_id, [{"content": "重启前接受的任务工作。"}])
    case.control.arm_match("task-before-record", "task.action.accepted_before_record", {"task_id": task["task_id"]})
    run = await _check(case, task)
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "task-before-record")
    paused = await _control(case, task, "pause")
    configuration = deepcopy(paused["configuration"])
    configuration["context"]["content"] = "task-updated-private：重启后不能重投新正文。"
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={"expected_revision": paused["revision"], "configuration": configuration})
    assert response.status_code == 200, response.text
    changed = response.json()["data"]
    if mode == "context_and_run":
        await _job(case, barrier["details"]["job_id"])
    before = (await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"]
    assert len(before) == 1 and before[0]["phase"] == "delivering" and before[0]["delivery"] is None
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "task-before-record"))
    actual = await _run(case, task, run)
    assert actual["delivery"]["item_id"] == barrier["details"]["item_id"]
    assert actual["delivery"]["state"] == ("saved" if mode == "context_only" else "accepted")
    if mode == "context_and_run":
        assert actual["delivery"]["execution_id"] == barrier["details"]["execution_id"]
        assert actual["delivery"]["job_id"] == barrier["details"]["job_id"]
        assert _wire_text(case.request(call)["messages"]).count("task-original-private") == 1
    else:
        call, = await case.plan(session_id, [{"content": "读取重启后保留的事件。"}])
        accepted = await _send(case, session_id, "读取任务原始事件。")
        await _job(case, accepted["job_id"])
        assert _wire_text(case.request(call)["messages"]).count("task-original-private") == 1
    assert "task-updated-private" not in _wire_text(case.requests())
    current = (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]
    assert (current["lifecycle"], current["revision"], current["definition_revision"]) == ("paused", 3, 2)
    assert current["configuration"] == changed["configuration"]
    assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(int(mode == "context_and_run"),)]
    assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(int(mode == "context_only"),)]
    assert case.call_count(session_id, thread_id) == 1
    await case.restart(hard=False)
    assert await _run(case, task, run) == actual
    assert case.call_count(session_id, thread_id) == 1
    case.write_actual("task-restart", {"barrier": barrier, "run": actual, "task": current,
                                        "pids": [backend.process.pid for backend in case.backends]})


@pytest.mark.asyncio
async def test_real_http_sse_delivers_authoritative_task_snapshots(restart_source_case: RestartRuntimeSourceCase) -> None:
    case = restart_source_case
    _session_id, _thread_id, target = await _target(case)
    task = await _create(case, _configuration(target))
    async with case.client.stream("GET", ROOT + "/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.aiter_lines()
        assert await anext(lines) == "event: snapshot"
        initial = await anext(lines)
        assert task["task_id"] in initial and '"lifecycle":"active"' in initial
        assert await anext(lines) == ""
        paused = await _control(case, task, "pause")
        assert await anext(lines) == "event: snapshot"
        latest = await anext(lines)
        assert paused["task_id"] in latest and '"lifecycle":"paused"' in latest


@pytest.mark.asyncio
async def test_slow_recovery_does_not_block_start_or_other_tasks_and_respects_pause(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    _session_id, _thread_id, target = await _target(case)
    first = await _create(case, _configuration(target), "first")
    second = await _create(case, _configuration(target), "second")
    runtime = case.container.persistent_task_runtime
    await runtime.stop()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime._process_task

    async def slow_recovery(task_id: str) -> None:
        if task_id == first["task_id"]:
            entered.set()
            await release.wait()
        await original(task_id)

    monkeypatch.setattr(runtime, "_process_task", slow_recovery)
    try:
        await asyncio.wait_for(runtime.start(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        response = await case.client.get(f"{ROOT}/{first['task_id']}")
        assert response.json()["data"]["recovering"] is True
        queued = await _check(case, first)
        paused = await _control(case, first, "pause")
        other = await _check(case, second)
        assert (await _run(case, second, other))["delivery"]["state"] == "saved"
        release.set()
        assert (await _run(case, first, queued, "suppressed"))["delivery"] is None
        current = (await case.client.get(f"{ROOT}/{first['task_id']}")).json()["data"]
        assert current["lifecycle"] == paused["lifecycle"] == "paused"
        assert current["revision"] == paused["revision"] and not current["recovering"]
        assert (await case.client.get("/api/v1/health")).status_code == 200
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initialization", "running"])
async def test_worker_failures_are_visible_and_shutdown_retrieves_the_failure(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    case = source_case
    runtime = case.container.persistent_task_runtime
    await runtime.stop()
    original = runtime.store.list

    def broken_store():
        raise RuntimeError("task-storage-fault")

    try:
        if stage == "running":
            await runtime.start()
        monkeypatch.setattr(runtime.store, "list", broken_store)
        if stage == "initialization":
            with pytest.raises(RuntimeError, match="task-storage-fault"):
                await runtime.start()
        else:
            runtime._wake.set()
            async with asyncio.timeout(3):
                while runtime._failure is None:
                    await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="task-storage-fault"):
            runtime.assert_healthy()
        response = await case.client.get("/api/v1/health")
        assert response.status_code == 500, response.text
        assert "task-storage-fault" in response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        with pytest.raises(RuntimeError, match="task-storage-fault"):
            await runtime.stop()
        assert runtime._worker is None and runtime._inflight == {}
    finally:
        monkeypatch.setattr(runtime.store, "list", original)
        if runtime._worker is not None:
            with pytest.raises(RuntimeError, match="task-storage-fault"):
                await runtime.stop()
        await runtime.start()


@pytest.mark.asyncio
async def test_slow_goal_recovery_shares_the_worker_and_does_not_block_other_tasks_or_pause(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    task = await _create(case, _configuration(target), "during-goal-recovery")
    runtime, goals = case.container.persistent_task_runtime, case.container.goal_service
    await runtime.stop()
    goal = await goals.set(session_id, objective="恢复期间暂停后不能迟到续跑")
    entered, release = asyncio.Event(), asyncio.Event()
    original = goals.get

    async def slow_goal(identity: str):
        current = asyncio.current_task()
        if identity == session_id and current is not None and current.get_name() == f"persistent-task:goal:{session_id}":
            entered.set()
            await release.wait()
        return await original(identity)

    monkeypatch.setattr(goals, "get", slow_goal)
    try:
        await asyncio.wait_for(runtime.start(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        # 通用任务继续通过真实 API 发布上下文，不等待慢 Goal 恢复。
        run = await _check(case, task)
        assert (await _run(case, task, run))["delivery"]["state"] == "saved"
        response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"status": "paused"})
        assert response.status_code == 200, response.text
        paused = response.json()["data"]
        assert paused["goal_id"] == goal.goal_id and paused["status"] == "paused"
        release.set()
        async with asyncio.timeout(5):
            while f"goal:{session_id}" in runtime._inflight or runtime._application_work:
                await asyncio.sleep(0.01)
        current = (await case.client.get(f"/api/v1/sessions/{session_id}/goal")).json()["data"]
        assert current == paused
        assert case.call_count(session_id, thread_id) == 0 and case.requests() == []
        assert (await case.client.get("/api/v1/health")).status_code == 200
    finally:
        release.set()



@pytest.mark.asyncio
async def test_goal_activation_keeps_request_identity_and_completes_through_the_model(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, _target_uri = await _target(case)
    await case.plan(session_id, [
        _tool_reply("update_goal", {"status": "complete"}, "call_goal_done"),
        {"content": "目标已经完成"},
    ], thread_id=thread_id)
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "验证目标后调用 update_goal 完成"})
    assert response.status_code == 200, response.text
    wakeup, = case.sql(session_id, thread_id, "SELECT job_id,request_id,gateway_id FROM runtime_execution_wakeups")
    assert wakeup[1:] == (response.json()["request_id"], case.gateway_id)
    await _job(case, wakeup[0])
    goal = (await case.client.get(f"/api/v1/sessions/{session_id}/goal")).json()["data"]
    assert goal["goal_id"] == response.json()["data"]["goal_id"] and goal["status"] == "complete"
    assert case.sql(session_id, thread_id, "SELECT execution_origin,turn_id,outcome FROM executions") == [("internal", None, "completed")]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
    assert case.call_count(session_id, thread_id) == 2 and len(case.requests()) == 2
    # 显式验证 worker 排空，而不是把测试函数结束误记为生命周期完成。
    await asyncio.wait_for(case.container.persistent_task_runtime.stop(), 5)
    assert case.container.persistent_task_runtime._worker is None
