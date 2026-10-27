"""通过产品入口核对 Goal 的统一存储、冷恢复及 fork 发布边界。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.gateway.credentials import load_or_create_gateway_id
from app.schemas.internal_v2.session_context import SessionContextReadResultDTO
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_context_resource import (
    session_resource_identity,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _configuration,
    _create,
    _release_match,
    _sql,
    _target,
)
from tests.integration.backend.sessions.test_runtime_context_publication import (
    fork_deadlines as fork_deadlines,  # noqa: PLC0414 - 正式fork进程外保护
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.support.fork_deadline import ForkDeadlineRegistry


async def _goal(case, session_id: str) -> dict | None:
    response = await case.client.get(f"/api/v1/sessions/{session_id}/goal")
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_goal_real_backend_restart_keeps_identity_and_clear_does_not_resurrect(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={
        "objective": "冷恢复必须保留的目标", "status": "paused", "token_budget": 123,
    })
    assert response.status_code == 200, response.text
    original = response.json()["data"]
    assert original["thread_id"] == thread_id
    await case.restart(hard=True)
    assert await _goal(case, session_id) == original
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "重启后的修改"})
    assert response.status_code == 200, response.text
    edited = response.json()["data"]
    assert edited["goal_id"] == original["goal_id"] and edited["revision"] == original["revision"] + 1
    await case.restart(hard=False)
    assert await _goal(case, session_id) == edited
    response = await case.client.delete(f"/api/v1/sessions/{session_id}/goal")
    assert response.status_code == 200 and response.json()["data"]["cleared"]
    await case.restart(hard=True)
    assert await _goal(case, session_id) is None
    assert case.requests() == []
    case.write_actual("goal-cold-owner", {"original": original, "edited": edited})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True, "browser": True, "managed_gateway": True}], indirect=True)
async def test_fork_goal_install_publish_replay_and_abort_use_the_same_owner(
    source_case: RuntimeSourceCase, fork_deadlines, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    source_id, _, _ = await _target(case)
    response = await case.client.put(f"/api/v1/sessions/{source_id}/goal", json={
        "objective": "来源 Goal 必须保持独立", "status": "paused", "token_budget": 456,
    })
    assert response.status_code == 200, response.text
    source_goal = response.json()["data"]
    point = await case.client.get(f"/api/v1/sessions/{source_id}/fork-source-point", params={"mode": "full_rollout_copy"})
    assert point.status_code == 200, point.text
    frozen = point.json()["data"]
    request = {
        "idempotency_key": "goal-owner-fork", "source_uri": frozen["source_uri"],
        "source_view_id": frozen["source_view_id"], "source_view_revision": frozen["source_view_revision"],
        "anchor_item_id": frozen["anchor_item_id"], "mode": "full_rollout_copy",
        "creation_kind": "new_session_main", "target_container_uri": frozen["main_container_uri"],
        "goal": {"token_budget": 78},
    }
    goals = case.container.goal_service
    install = goals.install_fork_goal
    prepared = []

    async def observe_install(record):
        result = await install(record)
        goal = goals._store.goal_read(record.session_id, creation=record)
        assert goal is not None and goal.goal_id == result
        assert record.session_id not in {current.session_id for current in goals.list_existing()}
        assert not (case.container.session_service.path_resolver.resolve_prepared_session_node(record) / "goal.json").exists()
        prepared.append(record)
        return result

    monkeypatch.setattr(goals, "install_fork_goal", observe_install)
    deadline = fork_deadlines.for_operation(source_id, request["idempotency_key"])
    try:
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{source_id}/fork-context", json=request), "Goal fork publish")
        assert response.status_code == 200, response.text
    finally:
        deadline.complete()
    forked = response.json()["data"]
    target_id = forked["session"]["session_id"]
    target = await _goal(case, target_id)
    assert target["goal_id"] == forked["goal_id"] != source_goal["goal_id"]
    assert target["thread_id"] == forked["thread_id"] and target["status"] == "paused"
    assert target["objective"] == source_goal["objective"] and target["token_budget"] == 78
    assert target["tokens_used"] == target["time_used_seconds"] == 0 and target["accounted_jobs"] == {}
    response = await case.client.put(f"/api/v1/sessions/{target_id}/goal", json={"objective": "发布后独立编辑"})
    assert response.status_code == 200, response.text
    edited = response.json()["data"]
    response = await case.client.post(f"/api/v1/sessions/{source_id}/fork-context", json=request)
    assert response.status_code == 200 and response.json()["data"] == forked
    assert await _goal(case, target_id) == edited and len(prepared) == 1
    assert await _goal(case, source_id) == source_goal

    async def fail_after_install(record):
        await observe_install(record)
        raise RuntimeError("goal-installed-before-publish-fault")

    monkeypatch.setattr(goals, "install_fork_goal", fail_after_install)
    failed_request = {**request, "idempotency_key": "goal-owner-abort"}
    deadline = fork_deadlines.for_operation(source_id, failed_request["idempotency_key"])
    try:
        failed = await deadline.wait(case.client.post(f"/api/v1/sessions/{source_id}/fork-context", json=failed_request), "Goal fork abort")
        assert failed.status_code == 500, failed.text
        assert "goal-installed-before-publish-fault" in failed.json()["message"]
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
        record = prepared[-1]
        current = case.container.session_service.path_resolver.catalog_store.get_creation_record(record.session_creation_idempotency_key)
        assert current.state == "aborted"
        assert record.session_id not in {goal.session_id for goal in goals.list_existing()}
        with closing(case.container.persistent_task_runtime.store._store.connection()) as connection:
            assert connection.execute("SELECT retired_at FROM persistent_tasks WHERE owner_session_id=? AND application_type='goal'", (record.session_id,)).fetchone()[0] is not None
    finally:
        deadline.complete()
    assert case.requests() == []
    case.write_actual("goal-fork-owner", {"source": source_goal, "target": target, "edited": edited,
                                         "aborted_intent": json.loads(current.request_intent_json)})


@pytest.mark.asyncio
async def test_unified_management_and_real_http_sse_read_the_current_goal_owner(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    automation = await _create(case, _configuration(target))
    assert automation["application_type"] == "automation" and automation["application_version"] == "1"
    assert automation["goal"] is None
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={
        "objective": "统一列表中的目标", "status": "paused", "token_budget": 123,
    })
    assert response.status_code == 200, response.text
    original = response.json()["data"]
    goal_id = original["goal_id"]

    async def snapshot(lines) -> dict:
        async with asyncio.timeout(15):
            async for line in lines:
                if line.startswith("data: "):
                    return {row["task_id"]: row for row in json.loads(line[6:])["items"]}
        raise AssertionError("任务 SSE 提前结束")

    async with case.client.stream("GET", ROOT + "/events") as stream:
        assert stream.status_code == 200 and stream.headers["X-Request-ID"]
        lines = stream.aiter_lines()
        initial = await snapshot(lines)
        assert set(initial) == {automation["task_id"], goal_id}
        assert initial[goal_id]["goal"] == original and initial[goal_id]["configuration"] is None
        assert initial[goal_id]["application_type"] == "goal" and initial[goal_id]["application_version"] == "1"
        response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={
            "objective": "从右侧 Goal 入口编辑", "status": "blocked",
        })
        assert response.status_code == 200, response.text
        edited = response.json()["data"]
        latest = await snapshot(lines)
        while latest[goal_id]["goal"] != edited:
            latest = await snapshot(lines)
        projected = latest[goal_id]
        assert projected["lifecycle"] == "needs_attention" and projected["phase"] == "blocked"
        assert projected["definition_revision"] == initial[goal_id]["definition_revision"] + 1
        response = await case.client.get(f"{ROOT}/{goal_id}")
        assert response.status_code == 200 and response.json()["data"] == projected
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        response = await case.client.post(f"{ROOT}/{goal_id}/control", json={
            "expected_revision": projected["revision"], "action": "resume",
        })
        assert response.status_code == 400 and "会话 Goal 入口" in response.text
        assert await _goal(case, session_id) == edited
    await case.restart(hard=True)
    response = await case.client.get(f"{ROOT}/{goal_id}")
    assert response.status_code == 200 and response.json()["data"] == projected
    async with case.client.stream("GET", ROOT + "/events") as stream:
        lines = stream.aiter_lines()
        assert (await snapshot(lines))[goal_id]["goal"] == edited
        response = await case.client.delete(f"/api/v1/sessions/{session_id}/goal")
        assert response.status_code == 200 and response.json()["data"]["cleared"]
        latest = await snapshot(lines)
        while goal_id in latest:
            latest = await snapshot(lines)
        assert set(latest) == {automation["task_id"]}
    assert (await case.client.get(f"{ROOT}/{goal_id}")).status_code == 404
    assert await _goal(case, session_id) is None and case.call_count(session_id, thread_id) == 0
    assert case.requests() == []
    case.write_actual("goal-management-owner", {"original": original, "edited": edited, "projected": projected})


@pytest.fixture
def restart_fork_deadlines(restart_source_case: RestartRuntimeSourceCase) -> Iterator[ForkDeadlineRegistry]:
    registry = ForkDeadlineRegistry(restart_source_case.artifacts_dir)
    try:
        yield registry
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_source_case", [{"native_services": True}], indirect=True)
@pytest.mark.parametrize("phase", ["fork.goal.installed_before_publish", "copy.target.published"])
async def test_fork_goal_publication_hard_restart_preserves_original_creation(
    restart_source_case: RestartRuntimeSourceCase, restart_fork_deadlines: ForkDeadlineRegistry, phase: str,
) -> None:
    case = restart_source_case
    case.client.headers["X-BoxTeam-Gateway-Id"] = load_or_create_gateway_id(
        case.workspace.parent / "boxteam-home/state/gateway/identity.json",
    )
    source_id, _, _ = await _target(case)
    response = await case.client.put(f"/api/v1/sessions/{source_id}/goal", json={
        "objective": "硬重启前冻结的来源目标", "status": "paused", "token_budget": 456,
    })
    assert response.status_code == 200, response.text
    source_goal = response.json()["data"]
    response = await case.client.get(f"/api/v1/sessions/{source_id}/fork-source-point", params={"mode": "full_rollout_copy"})
    assert response.status_code == 200, response.text
    point = response.json()["data"]
    key = "goal-fork-publication-restart"
    creation_key = SessionContextForkService._creation_key(key)
    request = {
        "idempotency_key": key, "source_uri": point["source_uri"],
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "anchor_item_id": point["anchor_item_id"], "mode": "full_rollout_copy",
        "creation_kind": "new_session_main", "target_container_uri": point["main_container_uri"],
        "goal": {"token_budget": 78},
    }

    def snapshot() -> tuple[dict, dict]:
        catalog = case.workspace / ".boxteam/navigation/session-catalog.sqlite"
        with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            record = dict(connection.execute(
                "SELECT * FROM session_creation_records WHERE session_creation_idempotency_key=?", (creation_key,),
            ).fetchone())
        with sqlite3.connect((case.workspace / ".boxteam/state/workspace.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT task_id,application_json,retired_at FROM persistent_tasks WHERE owner_session_id=?",
                (record["session_id"],),
            ).fetchall()
        assert len(rows) == 1 and rows[0]["retired_at"] is None
        return record, dict(rows[0])

    case.control.arm(creation_key, phase)
    deadline = restart_fork_deadlines.for_operation(source_id, key)
    operation = asyncio.create_task(case.client.post(f"/api/v1/sessions/{source_id}/fork-context", json=request))
    try:
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, creation_key, phase), "Goal fork phase")
        original, installed = snapshot()
        assert original["state"] == ("preparing" if phase == "fork.goal.installed_before_publish" else "published")
        frozen_goal = json.loads(original["request_intent_json"])["goal_snapshot"]
        assert installed["task_id"] == frozen_goal["goal_id"] != source_goal["goal_id"]
        target_id = original["session_id"]
        visible = await case.client.get(f"/api/v1/sessions/{target_id}/goal")
        assert visible.status_code == (404 if original["state"] == "preparing" else 200), visible.text
        if visible.status_code == 200:
            assert visible.json()["data"]["goal_id"] == installed["task_id"]
        case.write_actual("goal-fork-before-hard-restart", {"phase": phase, "creation": original, "goal": installed})
        await deadline.wait(case.restart(hard=True, while_stopped=lambda: case.control.release(creation_key, phase)), "Goal fork cold recovery")
        interrupted, = await asyncio.gather(operation, return_exceptions=True)
        assert isinstance(interrupted, httpx.TransportError), interrupted
        published, recovered_task = snapshot()
        assert published["state"] == "published" and recovered_task == installed
        immutable = set(original) - {"state", "record_updated_at"}
        assert {name: published[name] for name in immutable} == {name: original[name] for name in immutable}
        restored = await _goal(case, target_id)
        assert restored is not None
        assert restored["goal_id"] == installed["task_id"] and restored["thread_id"] == original["main_thread_id"]
        assert all(restored[name] == value for name, value in json.loads(installed["application_json"]).items())
        assert restored["status"] == "paused" and restored["objective"] == source_goal["objective"]
        assert restored["token_budget"] == 78
        assert datetime.fromisoformat(restored["created_at"]) == datetime.fromisoformat(original["created_at"])
        assert restored["tokens_used"] == restored["time_used_seconds"] == 0 and restored["accounted_jobs"] == {}
        assert await _goal(case, target_id) == restored and await _goal(case, source_id) == source_goal
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{source_id}/fork-context", json=request), "Goal fork original retry")
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert (result["session"]["session_id"], result["thread_id"], result["goal_id"]) == (
            target_id, original["main_thread_id"], installed["task_id"],
        )
        assert snapshot() == (published, installed)
    finally:
        deadline.complete()
        case.control.release(creation_key, phase)
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    response = await case.client.put(f"/api/v1/sessions/{target_id}/goal", json={"objective": "发布后保留的用户编辑"})
    assert response.status_code == 200, response.text
    edited = response.json()["data"]
    assert edited["goal_id"] == installed["task_id"] and edited["revision"] == restored["revision"] + 1
    await case.restart(hard=True)
    assert await _goal(case, target_id) == edited and await _goal(case, source_id) == source_goal
    assert case.requests() == []
    case.write_actual("goal-fork-after-hard-restart", {"phase": phase, "creation": published, "goal": restored, "edited": edited})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["inflight_model", "completed_before_goal_dispatch"])
async def test_user_created_goal_without_a_goal_admission_root_recovers_after_hard_restart(
    restart_source_case: RestartRuntimeSourceCase, phase: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    create_call, user_call, complete_call, final_call = await case.plan(session_id, [
        _tool_reply("create_goal", {"objective": "用户消息中创建且尚未自动续跑的目标", "token_budget": 345},
                    "call_user_create_goal"),
        {"content": "用户首轮的目标准备工作"},
        _tool_reply("update_goal", {"status": "complete"}, "call_user_goal_recovery_complete"),
        {"content": "冷恢复后完成原目标"},
    ], thread_id=thread_id, usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})
    barrier_id = "user-goal-first-continuation"
    if phase == "inflight_model":
        case.control.arm(user_call, "model.response_ready")
    else:
        case.control.arm_match(barrier_id, "goal.continuation.before_dispatch", {"session_id": session_id})
    def release() -> None:
        if phase == "inflight_model":
            case.control.release(user_call, "model.response_ready")
        else:
            _release_match(case, barrier_id)

    try:
        sent = await _send(case, session_id, "请创建给定预算的 Goal，并持续完成它")
        if phase == "inflight_model":
            await asyncio.to_thread(case.control.acknowledged, user_call, "model.response_ready", 20)
        else:
            await asyncio.to_thread(case.control.acknowledged_match, barrier_id)
        # 只读核对产品已写的应用状态；完成后屏障持有原 Goal 锁，不能用 GET 等待该锁。
        with sqlite3.connect((case.workspace / ".boxteam/state/workspace.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            row, = connection.execute(
                "SELECT task_id,application_json FROM persistent_tasks WHERE owner_session_id=? AND retired_at IS NULL",
                (session_id,),
            ).fetchall()
        goal_id, application = row[0], json.loads(row[1])
        assert application["status"] == "active" and application["last_continued_job_id"] is None
        assert application["tokens_used"] == 0 and application["accounted_jobs"] == {}
        created = case.tool_result(create_call + ":tool-call:call_user_create_goal")
        assert created["goal_id"] == goal_id and created["token_budget"] == 345
        original, = _sql(case.workspace, session_id, thread_id,
                        "SELECT execution_id,job_id,turn_id,execution_origin,outcome FROM executions")
        assert original[1] == sent["job_id"] and original[3] == "user_turn"
        assert original[4] == ("unknown" if phase == "inflight_model" else "completed")
        assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
        assert case.call_count(session_id, thread_id) == 2
        before_models = _sql(case.workspace, session_id, thread_id,
                             "SELECT model_call_id,execution_id FROM model_calls ORDER BY created_at")
        assert before_models == ([(create_call, original[0])] if phase == "inflight_model" else
                                 [(create_call, original[0]), (user_call, original[0])])
        case.write_actual("user-goal-before-hard-restart", {"phase": phase, "goal_id": goal_id,
                                                          "application": application, "execution": original, "models": before_models})

        await case.restart(hard=True, while_stopped=release)
        ready_at = asyncio.get_running_loop().time()
        async with asyncio.timeout(5):
            while not (wakeups := _sql(case.workspace, session_id, thread_id,
                                      "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups")):
                await asyncio.sleep(0.05)
        wakeup, = wakeups
        # TODO：首次冷 prepare 已测13.43秒，性能另行优化；注册和完成共用原45秒 Job 验收预算。
        async with asyncio.timeout(45):
            while True:
                response = await case.client.get(f"/api/v1/jobs/{wakeup[1]}")
                assert response.status_code in {200, 404}, response.text
                if response.status_code == 200:
                    break
                await asyncio.sleep(0.05)
            await _job(case, wakeup[1])
        recovery_seconds = asyncio.get_running_loop().time() - ready_at
        async with asyncio.timeout(5):
            while True:
                goal = await _goal(case, session_id)
                assert goal is not None
                if goal["tokens_used"] == 11:
                    break
                await asyncio.sleep(0.05)
        assert goal["status"] == "complete"
        assert goal["goal_id"] == goal_id and goal["objective"] == created["objective"] and goal["token_budget"] == 345
        assert set(goal["accounted_jobs"]) == {wakeup[1]} and wakeup[0] == f"goal:{goal_id}:state"
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT execution_id,job_id,turn_id,execution_origin,outcome FROM executions ORDER BY created_at") == [
                        (*original[:4], "execution_lost" if phase == "inflight_model" else "completed"),
                        (wakeup[2], wakeup[1], None, "internal", "completed"),
                    ]
        assert _sql(case.workspace, session_id, thread_id, "SELECT turn_id FROM turn_records") == [(original[2],)]
        models = _sql(case.workspace, session_id, thread_id,
                      "SELECT model_call_id,execution_id FROM model_calls ORDER BY created_at")
        assert models == [*before_models, (complete_call, wakeup[2]), (final_call, wakeup[2])]
        assert case.call_count(session_id, thread_id) == 4
        await case.restart(hard=True)
        assert await _goal(case, session_id) == goal and case.call_count(session_id, thread_id) == 4
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups") == [wakeup]
        case.write_actual("user-goal-after-hard-restart", {"phase": phase, "goal": goal,
                                                         "execution": original, "wakeup": wakeup, "models": models,
                                                         "ready_to_job_complete_seconds": recovery_seconds})
    finally:
        release()
        if len(case.backends) > 1:
            traces = await case.client.get(f"/api/v1/sessions/{session_id}/traces", params={"thread_id": thread_id})
            assert traces.status_code == 200, traces.text
            case.write_actual("user-goal-runtime-final", {
                "phase": phase, "traces": traces.json(),
                "executions": _sql(case.workspace, session_id, thread_id,
                                   "SELECT execution_id,job_id,execution_origin,outcome FROM executions ORDER BY created_at"),
                "models": _sql(case.workspace, session_id, thread_id,
                               "SELECT model_call_id,execution_id,outcome FROM model_calls ORDER BY created_at"),
                "backend_logs": {path.name: path.read_text() for path in (case.workspace / ".boxteam/logs").glob("runtime-source-restart-*.stderr.log")},
            })


@pytest.mark.asyncio
async def test_goal_restart_before_continuation_acceptance_does_not_lose_activation(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    await case.plan(session_id, [
        _tool_reply("update_goal", {"status": "complete"}, "call_goal_restart_done"),
        {"content": "硬重启后完成原目标"},
    ], thread_id=thread_id)
    barrier_id = "goal-before-continuation"
    case.control.arm_match(barrier_id, "goal.continuation.before_dispatch", {"session_id": session_id})
    activation = asyncio.create_task(case.client.put(f"/api/v1/sessions/{session_id}/goal", json={
        "objective": "接受续跑前中断后仍须恢复并调用 update_goal 完成",
    }))
    try:
        barrier = await asyncio.to_thread(case.control.acknowledged_match, barrier_id)
        goal_id = barrier["details"]["goal_id"]
        with sqlite3.connect((case.workspace / ".boxteam/state/workspace.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT task_id,application_json FROM persistent_tasks WHERE owner_session_id=? AND retired_at IS NULL", (session_id,)).fetchone()
        assert row[0] == goal_id and json.loads(row[1])["status"] == "active"
        resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
        assert not (resolver.resolve_thread_node(session_id, thread_id) / "rollout/index.sqlite").exists()
        assert case.call_count(session_id, thread_id) == 0
        case.write_actual("goal-before-continuation-kill", {"barrier": barrier, "state": json.loads(row[1])})
        await case.restart(hard=True, while_stopped=lambda: _release_match(case, barrier_id))
        result, = await asyncio.gather(activation, return_exceptions=True)
        assert isinstance(result, httpx.TransportError), result
        async with asyncio.timeout(15):
            while True:
                goal = await _goal(case, session_id)
                assert goal["goal_id"] == goal_id
                if goal["status"] == "complete":
                    break
                await asyncio.sleep(0.05)
        wakeups = _sql(case.workspace, session_id, thread_id, "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups")
        assert len(wakeups) == 1 and wakeups[0][0] == f"goal:{goal_id}:state"
        await _job(case, wakeups[0][1])
        assert case.call_count(session_id, thread_id) == 2
        assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
        await case.restart(hard=True)
        assert (await _goal(case, session_id))["goal_id"] == goal_id
        assert (await _goal(case, session_id))["status"] == "complete"
        assert _sql(case.workspace, session_id, thread_id, "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups") == wakeups
        assert case.call_count(session_id, thread_id) == 2
        case.write_actual("goal-after-continuation-restart", {"goal": goal, "wakeups": wakeups})
    finally:
        _release_match(case, barrier_id)
        if not activation.done():
            activation.cancel()
        await asyncio.gather(activation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unchanged", "pause", "objective"])
async def test_goal_inflight_model_restart_preserves_loss_pause_and_new_objective_ownership(
    restart_source_case: RestartRuntimeSourceCase,
    operation: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    stalled_call, = await case.plan(session_id, [{"content": "进程中断前尚未返回的旧目标回复"}], thread_id=thread_id)
    case.control.arm(stalled_call, "model.response_ready")
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "原目标的在途执行"})
    assert response.status_code == 200, response.text
    original_goal = response.json()["data"]
    try:
        await asyncio.to_thread(case.control.acknowledged, stalled_call, "model.response_ready", 20)
        original, = _sql(case.workspace, session_id, thread_id,
                        "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups")
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT execution_id,outcome FROM executions") == [(original[2], "unknown")]
        if operation == "pause":
            response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"status": "paused"})
            assert response.status_code == 200, response.text
        elif operation == "objective":
            await case.plan(session_id, [
                _tool_reply("update_goal", {"status": "complete"}, "call_goal_new_objective_done"),
                {"content": "新目标在原执行丢失后独立完成"},
            ], thread_id=thread_id, usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})
            response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "明确修改后的新目标"})
            assert response.status_code == 200, response.text
            changed = response.json()["data"]
            assert changed["goal_id"] == original_goal["goal_id"]
            assert changed["revision"] == original_goal["revision"] + 1
            assert case.call_count(session_id, thread_id) == 1
        await case.restart(hard=True, while_stopped=lambda: case.control.release(stalled_call, "model.response_ready"))
        expected = {"unchanged": "blocked", "pause": "paused", "objective": "complete"}[operation]
        async with asyncio.timeout(15):
            while (goal := await _goal(case, session_id))["status"] != expected:
                await asyncio.sleep(0.05)
        assert goal["goal_id"] == original_goal["goal_id"]
        # Job 注册表不恢复已丢失的旧任务；通过公开持久 Trace 核对原执行的失败归属。
        response = await case.client.get(f"/api/v1/sessions/{session_id}/traces", params={"thread_id": thread_id})
        assert response.status_code == 200, response.text
        loss, = [event for event in response.json()["data"]["items"]
                  if event["job_id"] == original[1] and event["type"] == "session_interrupted"]
        assert loss["thread_id"] == thread_id
        assert loss["raw"]["payload"]["code"] == "execution_lost"
        assert loss["raw"]["payload"]["phase"] == "process_exit"
        wakeups = _sql(case.workspace, session_id, thread_id,
                       "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups ORDER BY message_created_at")
        assert wakeups[0] == original
        outcomes = dict(_sql(case.workspace, session_id, thread_id, "SELECT execution_id,outcome FROM executions"))
        assert outcomes[original[2]] == "execution_lost"
        if operation == "objective":
            assert len(wakeups) == 2 and goal["objective"] == changed["objective"]
            await _job(case, wakeups[1][1])
            assert dict(_sql(case.workspace, session_id, thread_id,
                             "SELECT execution_id,outcome FROM executions")) == {
                                 original[2]: "execution_lost", wakeups[1][2]: "completed",
                             }
            async with asyncio.timeout(5):
                while (goal := await _goal(case, session_id))["tokens_used"] != 11:
                    await asyncio.sleep(0.05)
            assert set(goal["accounted_jobs"]) == {wakeups[1][1]}
        else:
            assert len(wakeups) == 1 and goal["tokens_used"] == 0
        count = 3 if operation == "objective" else 1
        assert case.call_count(session_id, thread_id) == count
        assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
        await case.restart(hard=True)
        assert (await _goal(case, session_id))["status"] == expected
        assert case.call_count(session_id, thread_id) == count
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups ORDER BY message_created_at") == wakeups
        case.write_actual(f"goal-inflight-{operation}", {"goal": goal, "original": original, "wakeups": wakeups})
    finally:
        case.control.release(stalled_call, "model.response_ready")


@pytest.mark.asyncio
async def test_goal_pending_read_checkpoint_resumes_the_original_execution_without_replaying_the_model(
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    workspace_id = case.backends[-1].workspace_id
    identity = workspace_session_inventory_identity(workspace_id)
    uri = workspace_session_inventory_uri(workspace_id)
    read_call, complete_call, final_call = await case.plan(session_id, [
        _tool_reply("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
            "resource_identity": str(identity), "uri": uri, "scope": "workspace", "view": "inventory",
        }}, "call_goal_pending_read"),
        _tool_reply("update_goal", {"status": "complete"}, "call_goal_resumed_complete"),
        {"content": "沿原 checkpoint 读取后完成目标"},
    ], thread_id=thread_id, usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})
    read_tool = read_call + ":tool-call:call_goal_pending_read"
    case.control.arm(read_tool, "canonical.tool_result.before_commit")
    response = await case.client.put(f"/api/v1/sessions/{session_id}/goal", json={"objective": "读取本工作区会话目录后完成原 Goal"})
    assert response.status_code == 200, response.text
    goal_id = response.json()["data"]["goal_id"]
    try:
        await asyncio.to_thread(case.control.acknowledged, read_tool, "canonical.tool_result.before_commit", 20)
        original, = _sql(case.workspace, session_id, thread_id,
                        "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups")
        model, = _sql(case.workspace, session_id, thread_id,
                     "SELECT model_call_id,execution_id,assembly_id FROM model_calls")
        assert model[0] == read_call and model[1] == original[2]
        assembly, = _sql(case.workspace, session_id, thread_id, "SELECT assembly_id,request_hash FROM context_assemblies")
        assert assembly[0] == model[2]
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='tool_result'") == [(0,)]
        assert case.call_count(session_id, thread_id) == 1
        await case.restart(hard=True, while_stopped=lambda: case.control.release(read_tool, "canonical.tool_result.before_commit"))
        await _job(case, original[1])
        goal = await _goal(case, session_id)
        assert goal["goal_id"] == goal_id and goal["status"] == "complete"
        assert goal["tokens_used"] == 11 and set(goal["accounted_jobs"]) == {original[1]}
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups") == [original]
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT execution_id,outcome FROM executions") == [(original[2], "completed")]
        models = _sql(case.workspace, session_id, thread_id,
                      "SELECT model_call_id,execution_id,assembly_id FROM model_calls ORDER BY created_at")
        assert models[0] == model and [row[0] for row in models] == [read_call, complete_call, final_call]
        assert {row[1] for row in models} == {original[2]}
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT assembly_id,request_hash FROM context_assemblies ORDER BY created_at")[0] == assembly
        read_result = case.tool_result(read_tool)
        read = SessionContextReadResultDTO.model_validate(read_result)
        assert str(read.resource_identity) == str(identity) and read.uri == uri
        assert read.scope == "workspace" and read.view == "inventory" and read.partial_errors == []
        assert {item.resource_identity for item in read.items} == {session_resource_identity(workspace_id, session_id)}
        # 后继模型确实消费原工具结果，不能仅靠最终 Goal 状态证明恢复。
        messages = case.request(complete_call)["messages"]
        carrier, = [message for message in messages if message.get("tool_calls")]
        call, = carrier["tool_calls"]
        assert call["id"] == "call_goal_pending_read" and call["function"]["name"] == "invoke_extension_tool"
        result, = [message for message in messages if message.get("role") == "tool"]
        assert result["tool_call_id"] == call["id"] and json.loads(result["content"]) == read_result
        assert case.call_count(session_id, thread_id) == 3
        assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
        await case.restart(hard=True)
        assert await _goal(case, session_id) == goal
        assert case.call_count(session_id, thread_id) == 3
        case.write_actual("goal-pending-read-recovery", {"goal": goal, "execution": original, "models": models,
                                                       "assembly": assembly, "read_result": read_result})
    finally:
        case.control.release(read_tool, "canonical.tool_result.before_commit")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["accepted_before_record", "recorded"])
async def test_goal_restart_after_execution_before_continuation_record_reconciles_the_original_execution(
    restart_source_case: RestartRuntimeSourceCase,
    phase: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, _ = await _target(case)
    await case.plan(session_id, [
        {"content": "第一轮结束，目标仍需继续"},
        _tool_reply("update_goal", {"status": "complete"}, "call_goal_reconcile_done"),
        {"content": "恢复后继续并完成目标"},
    ], thread_id=thread_id, usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})
    barrier_id = "goal-accepted-before-record"
    case.control.arm_match(barrier_id, f"goal.continuation.{phase}", {"session_id": session_id})
    activation = asyncio.create_task(case.client.put(f"/api/v1/sessions/{session_id}/goal", json={
        "objective": "接受后执行结束但未登记续跑标记时仍能继续",
    }))
    try:
        barrier = await asyncio.to_thread(case.control.acknowledged_match, barrier_id)
        goal_id = barrier["details"]["goal_id"]
        original, = _sql(case.workspace, session_id, thread_id,
                        "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups")
        await _job(case, original[1])
        async with asyncio.timeout(5):
            while _sql(case.workspace, session_id, thread_id,
                        "SELECT execution_id,outcome FROM executions") != [(original[2], "completed")]:
                await asyncio.sleep(0.05)
        assert case.call_count(session_id, thread_id) == 1
        await case.restart(hard=True, while_stopped=lambda: _release_match(case, barrier_id))
        result, = await asyncio.gather(activation, return_exceptions=True)
        assert isinstance(result, httpx.TransportError), result
        async with asyncio.timeout(15):
            while True:
                goal = await _goal(case, session_id)
                assert goal["goal_id"] == goal_id
                if goal["status"] == "complete":
                    break
                await asyncio.sleep(0.05)
        wakeups = _sql(case.workspace, session_id, thread_id,
                       "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups ORDER BY message_created_at")
        assert len(wakeups) == 2 and wakeups[0] == original
        assert all(row[0] == f"goal:{goal_id}:state" for row in wakeups)
        await _job(case, wakeups[1][1])
        assert case.call_count(session_id, thread_id) == 3
        async with asyncio.timeout(5):
            while (goal := await _goal(case, session_id))["tokens_used"] != 22:
                await asyncio.sleep(0.05)
        assert set(goal["accounted_jobs"]) == {row[1] for row in wakeups}
        assert all(account["tokens"] == 11 for account in goal["accounted_jobs"].values())
        assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
        await case.restart(hard=True)
        assert (await _goal(case, session_id))["status"] == "complete"
        assert case.call_count(session_id, thread_id) == 3
        assert _sql(case.workspace, session_id, thread_id,
                    "SELECT source_id,job_id,execution_id FROM runtime_execution_wakeups ORDER BY message_created_at") == wakeups
        case.write_actual("goal-accepted-recovery", {"goal": goal, "original": original, "wakeups": wakeups})
    finally:
        _release_match(case, barrier_id)
        if not activation.done():
            activation.cancel()
        await asyncio.gather(activation, return_exceptions=True)
