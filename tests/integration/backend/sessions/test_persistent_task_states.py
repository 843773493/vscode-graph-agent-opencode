"""由公开任务、消息及fork入口生成状态，核对安全封存与真实冷恢复。"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.prompting import internal_message_factory
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _check,
    _configuration,
    _control,
    _create,
    _post,
    _release_match,
    _run,
    _sql,
    _target,
)
from tests.integration.backend.sessions.test_runtime_context_publication import (
    fork_deadlines as fork_deadlines,  # noqa: PLC0414 - 复用正式fork进程外保护
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
from tests.support.fork_deadline import ForkDeadlineRegistry


async def _notify(case, task: dict, key: str, status: str) -> dict:
    run = await _post(case, f"{ROOT}/{task['task_id']}/notifications", {
        "idempotency_key": key, "payload": {"status": status},
    })
    return await _run(case, task, run)


def _applied(case: RuntimeSourceCase, session_id: str, thread_id: str, task: dict) -> list[str]:
    saver = case.container.checkpointer
    return [source.revision for (identity,) in case.sql(session_id, thread_id,
            "SELECT assembly_id FROM context_assemblies ORDER BY rowid")
            for source in saver.get_context_assembly(session_id, thread_id, assembly_id=identity).seal_binding.source_revisions
            if source.source_id == f"persistent-task:{task['task_id']}"]


@pytest.mark.asyncio
async def test_state_coalesces_unchanged_input_and_accepts_a_b_a_without_fake_turn(source_case: RuntimeSourceCase) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["context"]["mode"] = "state"
    configuration["trigger"]["type_id"] = "builtin.external"
    task = await _create(case, configuration)
    first = await _notify(case, task, "ready-1", "ready")
    duplicate = await _notify(case, task, "ready-2", "ready")
    revision_a = first["delivery"]["source_revision"]
    assert revision_a == duplicate["delivery"]["source_revision"]
    assert first["delivery"]["item_id"] != duplicate["delivery"]["item_id"]
    assert await case.container.job_service.list(session_id=session_id) == []
    assert case.sql(session_id, thread_id, "SELECT latest_revision,latest_visible_committed_revision FROM context_source_control_states") == [(revision_a, None)]
    calls = await case.plan(session_id, [{"content": "状态已读取。"} for _ in range(4)])
    expected = []
    first_consumption = None
    for index, status in enumerate((None, "ready", "offline", "ready")):
        if status is not None:
            publication = await _notify(case, task, f"update-{index}", status)
        else:
            publication = first
        accepted = await _send(case, session_id, f"第{index}次读取服务状态。")
        await _job(case, accepted["job_id"])
        if index != 1:
            expected.append(publication["delivery"]["source_revision"])
        assert _applied(case, session_id, thread_id, task) == expected
        response = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{publication['run_id']}")
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["consumption"] is not None
        assert detail["run"]["delivery"]["source_revision"] == publication["delivery"]["source_revision"]
        if index == 0:
            first_consumption = detail["consumption"]
        original = await case.client.get(f"{ROOT}/{task['task_id']}/runs/{first['run_id']}")
        assert original.status_code == 200, original.text
        assert original.json()["data"]["consumption"] == first_consumption
        if index == 2:
            assert publication["delivery"]["source_revision"] != revision_a
        if index == 3:
            assert publication["delivery"]["source_revision"] != revision_a
    assert _wire_text(case.request(calls[0])["messages"]).count("task-original-private") == 1
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(4,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
    assert case.sql(session_id, thread_id,
        "SELECT COUNT(*) FROM context_view_items WHERE item_id GLOB 'item-rstate_*'") == [(0,)]
    assert len(case.requests()) == 4
    case.write_actual("state-coalescing", {"first": first, "duplicate": duplicate, "requests": case.requests()})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_only", "context_and_run"])
async def test_state_publication_and_wakeup_survive_accepted_before_record_hard_restart(
    restart_source_case: RestartRuntimeSourceCase, mode: str,
) -> None:
    case = restart_source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target, mode)
    configuration["context"]["mode"] = "state"
    task = await _create(case, configuration)
    if mode == "context_and_run":
        call, = await case.plan(session_id, [{"content": "按发布状态执行。"}])
    case.control.arm_match("state-before-record", "task.action.accepted_before_record", {"task_id": task["task_id"]})
    run = await _check(case, task)
    barrier = await asyncio.to_thread(case.control.acknowledged_match, "state-before-record")
    paused = await _control(case, task, "pause")
    changed = deepcopy(paused["configuration"])
    changed["context"]["content"] = "state-current-definition-must-not-replace-frozen-input"
    await _post_update(case, task, paused["revision"], changed)
    if mode == "context_and_run":
        await _job(case, barrier["details"]["job_id"])
    await case.restart(hard=True, while_stopped=lambda: _release_match(case, "state-before-record"))
    actual = await _run(case, task, run)
    assert actual["delivery"]["item_id"] == barrier["details"]["item_id"]
    if mode == "context_only":
        call, = await case.plan(session_id, [{"content": "读取原状态。"}])
        accepted = await _send(case, session_id, "读取原状态。")
        await _job(case, accepted["job_id"])
    text = _wire_text(case.request(call)["messages"])
    assert text.count("task-original-private") == 1 and changed["context"]["content"] not in text
    assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM item_catalog WHERE item_id GLOB 'item-rstate_*'") == [(1,)]
    assert _sql(case.workspace, session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(int(mode == "context_and_run"),)]
    await case.restart(hard=False)
    second, = await case.plan(session_id, [{"content": "重启后的状态不重复注入。"}])
    accepted = await _send(case, session_id, "再次读取状态。")
    await _job(case, accepted["job_id"])
    assert _wire_text(case.request(second)["messages"]).count("task-original-private") == 1
    assert await _run(case, task, run) == actual
    assert case.call_count(session_id, thread_id) == 2
    case.write_actual("state-hard-restart", {"barrier": barrier, "run": actual, "requests": case.requests()})


async def _post_update(case, task: dict, revision: int, configuration: dict) -> dict:
    response = await case.client.put(f"{ROOT}/{task['task_id']}", json={
        "expected_revision": revision, "configuration": configuration,
    })
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True, "terminal": True, "browser": True}], indirect=True)
async def test_state_fork_keeps_exact_historical_body_and_rebuilds_from_its_canonical_owner(
    source_case: RuntimeSourceCase, fork_deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session_id, thread_id, target = await _target(case)
    configuration = _configuration(target)
    configuration["context"]["mode"] = "state"
    task = await _create(case, configuration)
    original = await _run(case, task, await _check(case, task))
    await case.plan(session_id, [{"content": "已读取历史状态。"}])
    user = await _send(case, session_id, "读取服务状态。")
    await _job(case, user["job_id"])
    updated = deepcopy(task["configuration"])
    updated["context"]["content"] = "state-source-later-private"
    changed = await _post_update(case, task, task["revision"], updated)
    later = await _run(case, changed, await _check(case, changed, "later-state"))
    item_id, = case.sql(session_id, thread_id,
        "SELECT item_id FROM item_catalog WHERE semantic_kind='assistant_output' ORDER BY item_sequence DESC LIMIT 1")[0]
    history = case.container.checkpointer.load_history(session_id, thread_id, TurnHistoryLoadRequest(direction="tail"))
    deadline = fork_deadlines.for_operation(session_id, "runtime-state-fork")
    try:
        point = await deadline.wait(case.client.get(f"/api/v1/sessions/{session_id}/fork-source-point", params={
            "thread_id": thread_id, "item_id": item_id, "mode": "view_prefix_fork",
            "source_view_id": history.source_view_id, "source_view_revision": history.source_view_revision,
        }), "state source point")
        assert point.status_code == 200, point.text
        frozen = point.json()["data"]
        forked = await deadline.wait(_post(case, f"/api/v1/sessions/{session_id}/fork-context", {
            "idempotency_key": "state-history-fork", "source_uri": frozen["source_uri"],
            "source_view_id": frozen["source_view_id"], "source_view_revision": frozen["source_view_revision"],
            "anchor_item_id": frozen["anchor_item_id"], "mode": "view_prefix_fork",
            "creation_kind": "new_session_main", "target_container_uri": frozen["main_container_uri"],
        }), "state public fork")
    finally:
        deadline.complete()
    target_session = forked["session"]["session_id"]
    target_thread = forked["thread_id"]
    assert case.sql(target_session, target_thread, "SELECT COUNT(*) FROM item_catalog WHERE item_id=?",
                    (original["delivery"]["item_id"],)) == [(1,)]
    assert case.sql(target_session, target_thread, "SELECT COUNT(*) FROM item_catalog WHERE item_id=?",
                    (later["delivery"]["item_id"],)) == [(0,)]
    call, = await case.plan(target_session, [{"content": "只读取fork历史版本。"}], thread_id=target_thread)
    accepted = await _send(case, target_session, "读取fork中的状态。")
    await _job(case, accepted["job_id"])
    text = _wire_text(case.request(call)["messages"])
    assert text.count("task-original-private") == 1 and "state-source-later-private" not in text
    assert _applied(case, target_session, target_thread, task)[-1] == original["delivery"]["source_revision"]
    case.write_actual("state-fork", {"original": original, "fork": forked, "request": case.request(call)})


@pytest.mark.asyncio
async def test_state_and_wakeup_rollback_together_and_retry_does_not_revert_newer_state(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id, thread_id, _target_uri = await _target(case)
    orchestrator = case.container.session_orchestrator
    message = internal_message_factory.build(kind="checkpoint_reminder", control="atomic-state-original-private")
    arguments = {"thread_id": thread_id, "source_id": "state:rollback", "idempotency_key": "atomic-state", "context_mode": "state"}
    writer = case.container.checkpointer._storage.checkpoints
    original = writer._apply_source_lifecycle_decision_transaction

    def fail_after_state(connection, decision, item):
        result = original(connection, decision, item)
        if decision.source_kind == "runtime_state":
            raise RuntimeError("state-written-before-wakeup-fault")
        return result

    # 故障截在真实事务内，先走原状态写入，再验证准入、队列和状态均未发布。
    with monkeypatch.context() as fault:
        fault.setattr(writer, "_apply_source_lifecycle_decision_transaction", fail_after_state)
        with pytest.raises(RuntimeError, match="state-written-before-wakeup-fault"):
            await orchestrator.submit_runtime_event(session_id, message, **arguments)
    for table in ("item_catalog", "context_source_control_states", "executions", "execution_queue", "runtime_execution_wakeups"):
        assert case.sql(session_id, thread_id, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert await case.container.job_service.list(session_id=session_id) == []
    call, = await case.plan(session_id, [{"content": "状态与执行共同接受。"}])
    accepted = await orchestrator.submit_runtime_event(session_id, message, **arguments)
    await _job(case, accepted.job_id)
    assert _wire_text(case.request(call)["messages"]).count("atomic-state-original-private") == 1
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM item_catalog WHERE item_id GLOB 'item-rstate_*'") == [(1,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(1,)]
    frozen = case.sql(session_id, thread_id, "SELECT execution_id,job_id,source_item_id FROM runtime_execution_wakeups")
    newer = await orchestrator.publish_runtime_context(session_id,
        internal_message_factory.build(kind="checkpoint_reminder", control="atomic-state-newer-private"),
        thread_id=thread_id, source_id="state:rollback", idempotency_key="atomic-newer", context_mode="state")
    retried = await orchestrator.submit_runtime_event(session_id, message, **arguments)
    assert (retried.message_id, retried.job_id) == (accepted.message_id, accepted.job_id)
    assert case.sql(session_id, thread_id, "SELECT execution_id,job_id,source_item_id FROM runtime_execution_wakeups") == frozen
    assert case.sql(session_id, thread_id, "SELECT latest_revision FROM context_source_control_states WHERE source_id='state:rollback'") == [(newer.revision,)]
    assert case.call_count(session_id, thread_id) == 1
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(0,)]
    case.write_actual("state-atomic-retry", {"accepted": accepted.model_dump(mode="json"), "retry": retried.model_dump(mode="json")})
