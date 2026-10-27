"""通过产品入口核对 Goal 的统一存储、冷恢复及 fork 发布边界。"""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_context_publication import (
    fork_deadlines as fork_deadlines,  # noqa: PLC0414 - 正式fork进程外保护
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离
)


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
