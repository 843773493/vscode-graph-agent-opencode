"""会话动作从真实任务入口创建，核对 canonical 身份、并发等待与硬重启。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.core.path_utils import get_session_path_resolver
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
    _send,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用夹具，按当前文件隔离
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用夹具，按当前文件隔离
)


def generation_configuration(workspace_id: str, *, mode: str = "new_per_run", target: dict | None = None) -> dict:
    return {
        "name": "每日检查", "trigger": {"type_id": "builtin.manual", "version": "1"},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
        "action": {"type_id": "builtin.session_generation", "version": "1", "generation": {
            "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
            "config": {"prompt": "generation-original-private：检查当前状态，不调用工具。"},
            "placement": {"kind": "workspace", "workspace_id": workspace_id},
            "context_source": {"kind": "fresh"},
            "session_strategy": {"mode": mode, "target": target},
            "title_template": "{generator.name}", "path_template": ["自动化"],
        }},
        "policies": {"completion": "continuous", "match_mode": "each_match"},
    }


async def generation_run(case: ModelReplayCase, task: dict, run: dict, *, accepted: bool = False) -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get(f"{ROOT}/{task['task_id']}/runs")
            assert response.status_code == 200, response.text
            actual, = [row for row in response.json()["data"]["items"] if row["run_id"] == run["run_id"]]
            if accepted and actual["delivery"] is not None:
                return actual
            if actual["phase"] not in {"pending", "checking", "delivering"}:
                assert actual["phase"] == "delivered", actual
                return actual
            await asyncio.sleep(0.02)


def generation_state(case: ModelReplayCase, run: dict) -> dict:
    database = case.workspace / ".boxteam/state/workspace.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        raw, = connection.execute("SELECT generation_json FROM persistent_task_runs WHERE run_id=?", (run["run_id"],)).fetchone()
    return json.loads(raw)


def admission_rows(case: ModelReplayCase, state: dict) -> list[tuple]:
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    database = resolver.resolve_thread_node(state["session_id"], state["thread_id"]) / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute("SELECT job_id,turn_id,admission_idempotency_key FROM executions ORDER BY created_at").fetchall()


@pytest.mark.asyncio
async def test_generation_validation_dry_run_and_same_task_edit(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, _uri = await _target(case)
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    configuration = generation_configuration(workspace_id, mode="continue_existing", target={
        "workspace_id": workspace_id, "session_id": session_id, "thread_id": thread_id,
    })
    for field, value in (("generator_type", {"type_id": "missing", "version": "1"}), ("config", {"prompt": 123}),
                         ("placement", {"kind": "workspace", "workspace_id": "different"})):
        invalid = deepcopy(configuration)
        invalid["action"]["generation"][field] = value
        response = await case.client.post(ROOT, json={"idempotency_key": "invalid", "configuration": invalid})
        assert response.status_code == 400, response.text
    task = await _create(case, configuration)
    assert await _create(case, configuration) == task
    trial = await _check(case, task, check_only=True)
    result = await _run(case, task, trial, "checked")
    assert result["generation"] is None and result["delivery"] is None
    assert case.requests() == []
    changed = deepcopy(task["configuration"])
    changed["context"]["content"] = "附加材料"
    changed["condition"] = {"type_id": "builtin.rule", "version": "1", "rule": {"pointer": "/trigger/type", "expected": "manual"}}
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={"expected_revision": task["revision"], "configuration": changed})
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["task_id"] == task["task_id"] and updated["definition_revision"] == 2
    assert len((await case.client.get(f"{ROOT}/{task['task_id']}/runs")).json()["data"]["items"]) == 1


@pytest.mark.asyncio
async def test_generation_wait_releases_worker_and_preserves_overlap_and_busy_queue(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, _uri = await _target(case)
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    configuration = generation_configuration(workspace_id, mode="continue_existing", target={
        "workspace_id": workspace_id, "session_id": session_id, "thread_id": thread_id,
    })
    task = await _create(case, configuration)
    calls = await case.plan(session_id, [{"content": "第一项完成"}, {"content": "第二项完成"}])
    case.control.arm(calls[0], "model.response_ready")
    try:
        first = await _check(case, task, "first")
        await asyncio.to_thread(case.control.acknowledged, calls[0], "model.response_ready")
        first = await generation_run(case, task, first, accepted=True)
        second = await _check(case, task, "second")
        second = await generation_run(case, task, second, accepted=True)
        assert first["generation"]["job_id"] != second["generation"]["job_id"]
        assert second["phase"] == "delivering"
        assert case.call_count(session_id, thread_id) == 1
        probe = await _create(case, _configuration(_uri), key="worker-probe")
        checked = await _check(case, probe)
        assert (await _run(case, probe, checked))["delivery"]["state"] == "saved"
        await _control(case, task, "pause")
    finally:
        case.control.release(calls[0], "model.response_ready")
    for run in (first, second):
        final = await generation_run(case, task, run)
        assert final["generation"]["status"] == "completed"
        state = generation_state(case, run)
        assert state["prepared_message"] is None
        assert state["execution_identity"]["job_id"] == run["generation"]["job_id"]
        response = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{run['run_id']}")
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["work_target"]["session_id"] == session_id and detail["work_target"]["thread_id"] == thread_id
        assert "generation-original-private" in detail["action_content"]
        assert detail["execution_outcome"] == "completed" and detail["consumption"] is not None
    rows = admission_rows(case, state)
    assert len(rows) == 2 and len({row[0] for row in rows}) == len({row[1] for row in rows}) == 2
    for call in calls:
        assert _wire_text(case.request(call)["messages"]).count("generation-original-private") in {1, 2}
    assert (await case.client.get(f"{ROOT}/{task['task_id']}")).json()["data"]["lifecycle"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["task.generation.session_created_before_record", "task.generation.prepared_before_accept", "task.generation.accepted_before_record"])
async def test_generation_hard_restart_reuses_session_message_and_execution(
    restart_source_case: RestartRuntimeSourceCase, phase: str,
) -> None:
    case = restart_source_case
    configuration = generation_configuration(case.client.headers["X-BoxTeam-Workspace-Id"])
    task = await _create(case, configuration)
    barrier = "generation-crash"
    case.control.arm_match(barrier, phase, {"task_id": task["task_id"]})
    case.control.arm_match("generation-model", "thread.runtime.acquired", {"execution_origin": "user_turn"})
    run = await _check(case, task)
    await asyncio.to_thread(case.control.acknowledged_match, barrier)
    before = generation_state(case, run)
    expected_session = before["session_id"]
    if phase == "task.generation.session_created_before_record":
        assert expected_session is None and before["execution_identity"] is None
        catalog = (await case.client.get("/api/v1/session-catalog/export")).json()["data"]["items"]
        generated, = [row for row in catalog if row["session"] and row["session"]["generation_origin"]]
        expected_session = generated["session_id"]
    calls = await case.plan(expected_session, [{"content": "重启后只执行一次"}])
    try:
        await case.restart(hard=True)
    finally:
        _release_match(case, barrier)
        _release_match(case, "generation-model")
    final = await generation_run(case, task, run)
    after = generation_state(case, run)
    fields = ("title", "navigation_path") if phase == "task.generation.session_created_before_record" else ("session_id", "thread_id", "message_id", "execution_identity", "title", "navigation_path")
    for field in fields:
        assert after[field] == before[field]
    assert after["session_id"] == expected_session
    assert after["prepared_message"] is None
    identity = after["execution_identity"]
    assert final["generation"]["job_id"] == identity["job_id"]
    assert admission_rows(case, after) == [(identity["job_id"], identity["turn_id"], identity["admission_idempotency_key"])]
    catalog = (await case.client.get("/api/v1/session-catalog/export")).json()["data"]["items"]
    generated = [row for row in catalog if row["session"] and row["session"]["generation_origin"]]
    assert [row["session_id"] for row in generated] == [expected_session]
    assert case.call_count(after["session_id"], after["thread_id"]) == 1
    assert "generation-original-private" in _wire_text(case.request(calls[0])["messages"])
    assert not (case.workspace / ".boxteam/generation-runs").exists()
    assert not list((case.workspace / ".boxteam/sessions").rglob("generation_intents"))


@pytest.mark.asyncio
async def test_import_retains_deleted_terminal_history_and_repeat_does_not_overwrite_edits(source_case: RuntimeSourceCase) -> None:
    case = source_case
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    original = {
        "task_id": "gen_historical", "source_hash": "a" * 64, "revision": 7, "lifecycle": "paused",
        "configuration": generation_configuration(workspace_id), "workspace_ids": {"old_route": workspace_id},
        "created_at": "2026-10-26T00:00:00Z", "updated_at": "2026-10-26T01:00:00Z",
        "runs": [{"run_id": "grun_historical", "generator_id": "gen_historical", "idempotency_key": "legacy-key", "status": "completed",
                  "trigger_type": "manual", "scheduled_for": "2026-10-26T01:00:00Z", "job_id": "job_historical", "message_id": "msg_historical",
                  "outputs": [{"workspace_id": "old_route", "session_id": "ses_deleted", "title": "保留的历史标题", "navigation_path": ["原目录"]}]}],
    }
    endpoint = ROOT + "/imports/session-generators"
    installed = await case.client.post(endpoint, json=original)
    assert installed.status_code == 200, installed.text
    task = (await case.client.get(ROOT + "/gen_historical")).json()["data"]
    assert task["revision"] == 7 and task["lifecycle"] == "paused"
    run, = (await case.client.get(ROOT + "/gen_historical/runs")).json()["data"]["items"]
    assert run["phase"] == "delivered" and run["trigger_key"] == "legacy-key"
    assert run["generation"]["job_id"] == "job_historical" and run["generation"]["message_id"] == "msg_historical"
    assert run["generation"]["outputs"] == [{"kind": "session", "workspace_id": workspace_id, "session_id": "ses_deleted", "title": "保留的历史标题", "navigation_path": ["原目录"]}]
    configuration = task["configuration"]
    configuration["name"] = "导入后修改"
    changed = await case.client.put(ROOT + "/gen_historical", json={"expected_revision": 7, "configuration": configuration})
    assert changed.status_code == 200, changed.text
    assert (await case.client.post(endpoint, json=original)).status_code == 200
    assert (await case.client.get(ROOT + "/gen_historical")).json()["data"] == changed.json()["data"]
    conflict = await case.client.post(endpoint, json={**original, "source_hash": "b" * 64})
    assert conflict.status_code == 409, conflict.text
    assert case.requests() == []


@pytest.mark.asyncio
async def test_import_inflight_generation_reuses_original_canonical_acceptance(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, _uri = await _target(case)
    workspace_id = case.client.headers["X-BoxTeam-Workspace-Id"]
    calls = await case.plan(session_id, [{"content": "迁移后原执行完成"}])
    case.control.arm(calls[0], "model.response_ready")
    try:
        accepted = await _send(case, session_id, "迁移前已接受的唯一消息")
        await asyncio.to_thread(case.control.acknowledged, calls[0], "model.response_ready")
        admission = case.container.checkpointer.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=accepted["job_id"],
        )
        identity = {"job_id": admission.job_id, "turn_id": admission.turn_id,
                    "admission_idempotency_key": admission.admission_idempotency_key}
        task_id, run_id, trigger_key = "gen_inflight", "grun_inflight", "legacy-inflight"
        configuration = generation_configuration(workspace_id, mode="continue_existing", target={
            "workspace_id": workspace_id, "session_id": session_id, "thread_id": thread_id,
        })
        generation = configuration["action"]["generation"]
        legacy_request = {key: generation[key] for key in ("generator_type", "config", "placement", "context_source", "session_strategy")}
        legacy_request.update(name=configuration["name"], execution_workspace_id=workspace_id,
                              generator_id=task_id, run_id=run_id, idempotency_key=trigger_key,
                              title="原会话", navigation_path=[])
        directory = hashlib.sha256(task_id.encode()).hexdigest()
        digest = hashlib.sha256(f"{task_id}\n{trigger_key}".encode()).hexdigest()
        ledger = case.workspace / ".boxteam/generation-runs" / directory / f"{digest}.json"
        ledger.parent.mkdir(parents=True)
        # 旧格式仅作为迁移输入；消息、模型和准入均由上面的真实产品入口生成。
        ledger.write_text(json.dumps({
            "request": legacy_request, "generated_session_id": session_id,
            "prepared_message_id": accepted["message_id"], "dispatched_execution_identity": identity,
            "phase": "child_dispatched", "status": "executing",
        }), encoding="utf-8")
        now = datetime.now(UTC).isoformat()
        imported = await case.client.post(ROOT + "/imports/session-generators", json={
            "task_id": task_id, "source_hash": "c" * 64, "revision": 3, "lifecycle": "paused",
            "configuration": configuration, "workspace_ids": {workspace_id: workspace_id},
            "created_at": now, "updated_at": now,
            "runs": [{"run_id": run_id, "generator_id": task_id, "idempotency_key": trigger_key,
                      "status": "running", "trigger_type": "manual", "scheduled_for": now,
                      "message_id": accepted["message_id"], "job_id": accepted["job_id"]}],
        })
        assert imported.status_code == 200, imported.text
        assert not ledger.exists()
        assert (case.workspace / ".boxteam/migrations/session-generators" / directory / ledger.relative_to(case.workspace)).is_file()
        task = (await case.client.get(ROOT + "/" + task_id)).json()["data"]
        assert task["lifecycle"] == "paused" and task["revision"] == 3
        run = await generation_run(case, task, {"run_id": run_id}, accepted=True)
        assert run["generation"]["message_id"] == accepted["message_id"]
        assert run["delivery"]["execution_id"] == admission.execution_id
        assert generation_state(case, run)["prepared_message"] is None
    finally:
        case.control.release(calls[0], "model.response_ready")
    final = await generation_run(case, task, run)
    state = generation_state(case, final)
    assert state["execution_identity"] == identity
    assert admission_rows(case, state) == [(identity["job_id"], identity["turn_id"], identity["admission_idempotency_key"])]
    assert case.call_count(session_id, thread_id) == 1
    assert (await case.client.get(ROOT + "/" + task_id)).json()["data"]["lifecycle"] == "paused"
