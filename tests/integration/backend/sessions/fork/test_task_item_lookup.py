"""真实模型 task 的全分支前缀定位与精确来源 owner。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from tests.integration.backend.sessions.fork.test_item_lookup import (
    LINEAGE_QUERY,
    BranchedItemHistory,
)
from tests.integration.backend.sessions.fork.test_item_lookup import (
    branched_item_history as branched_item_history,  # noqa: PLC0414 - 共用产品消息／回放生成的历史
)
from tests.integration.backend.sessions.fork.test_task_context import (
    _completed_parent,
    _first_response_item,
    _send_thread,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    deadlines as deadlines,  # noqa: PLC0414 - 保留正式60秒fork监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _tool_reply,
    _wait_until,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实服务与model_stream
)
from tests.support.e2e_control.runtime_ports import control_request
from tests.support.fork_deadline import ForkDeadlineRegistry

pytestmark = pytest.mark.parametrize("source_case", [{
    "managed_gateway": True, "terminal": True, "browser": True,
}], indirect=True)


async def _model_task_prefix(
    case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, history: BranchedItemHistory,
    *, caller: str, expected_view: str, explicit: bool, before: bool, label: str,
    observed_candidates: dict[str, str],
) -> dict[str, object]:
    session, item = history.session, history.item_id
    selection = {"mode": "view_prefix_fork", "item_id": item}
    if explicit:
        selection["view_id"] = expected_view
    if before:
        selection["anchor_mode"] = "before"
    comparison = "<" if before else "<="
    expected = case.sql(session, caller,
        "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 "
        f"AND context_ordinal{comparison}(SELECT context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) "
        "ORDER BY context_ordinal", (expected_view, expected_view, item))
    assert expected
    reports = case.sql(session, caller,
        "SELECT COUNT(*) FROM executions WHERE thread_id=? AND execution_origin='internal' AND outcome='completed'", (caller,))[0][0]
    barrier, call_id = "lookup-child-" + label, "call_lookup_" + label
    seed = "lookup-private-seed-" + label
    case.control.arm_match(barrier, "thread.runtime.acquired", {"session_id": session, "execution_origin": "internal"})
    await case.plan(session, [
        _tool_reply("task", {"description": seed, "context": selection}, call_id),
        {"content": "lookup-parent-dispatched-" + label}, {"content": "lookup-report-ack-" + label},
    ], thread_id=caller)
    deadline = deadlines.for_operation(target=caller, operation_key="model-task-" + label)
    accepted = await _send_thread(case, session, caller, "lookup-inflight-" + label)
    try:
        event = await deadline.wait(asyncio.to_thread(case.control.acknowledged_match, barrier), "真实task发布并准入child")
    except TimeoutError:
        failure = await case.client.get(f"/api/v1/jobs/{accepted['job_id']}")
        assert failure.status_code == 200, failure.text
        case.write_actual("task-before-child-" + label, failure.json())
        if failure.json()["data"]["status"] == "failed":
            # 已确认原Job终态，保留精确产品错误；其它未知操作仍由原watchdog监督。
            deadline.complete()
            pytest.fail("task未发布child，原Job已明确失败：" + failure.text)
        raise
    deadline.complete()
    child = event["details"]["thread_id"]
    try:
        # 屏障位于首次运行准备前；此时可以精确核继承成员，seed仍由原owner注入。
        inherited = case.sql(session, child,
            "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
            "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
            "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal")
        assert inherited == expected
        child_call, = await case.plan(session, [{"content": "lookup-child-result-" + label}], thread_id=child)
    finally:
        control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {"action": "release_match", "barrier_id": barrier})
    await _job(case, accepted["job_id"])
    with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
        intents = [intent for intent in owner.control_store.list_initial_execution_intents() if intent.thread_id == child]
        intent, = intents
        record = owner.control_store.get_thread_creation_record(intent.creation_idempotency_key)
        member = owner.control_store.get_collaboration_member(record.delegation_id)
    await _job(case, intent.job_id)
    await _wait_until(lambda: case.sql(session, caller,
        "SELECT COUNT(*) FROM executions WHERE thread_id=? AND execution_origin='internal' AND outcome='completed'", (caller,))[0][0] == reports + 1)
    capture = case.container.session_service.path_resolver.catalog_store.get_fork_source_capture(creation_key=record.thread_creation_idempotency_key)
    point = capture.captured_point
    # 活跃caller仍包含锚点时，真实task提交会创建更新候选；按提交后的原件核对。
    actual_expected_view = expected_view if explicit else observed_candidates[call_id]
    assert (point.source_view_id, point.anchor_item_id) == (actual_expected_view, item)
    if label == "nonancestor":
        assert actual_expected_view == expected_view
    if label == "caller":
        assert actual_expected_view != expected_view
    assert json.loads(capture.request_json)["anchor_mode"] == ("before" if before else "inclusive")
    request = json.loads(record.request_intent_json)
    assert (request["source_session_id"], request["source_thread_id"]) == (session, caller)
    assert json.loads(capture.request_json)["source_view_id"] == (expected_view if explicit else None)
    assert json.loads(record.task_seed)["context"] == {**selection, "view_id": selection.get("view_id"), "anchor_mode": "before" if before else "inclusive"}
    reference = json.loads(record.task_reference)
    assert reference == {"parent_session_id": session, "parent_thread_id": caller, "delegation_id": record.delegation_id}
    assert member.coordinator_thread_id == caller and member.child_thread_id == child
    wire = _wire_text(case.request(child_call)["messages"])
    for marker in ("lookup-first-question", "lookup-first-answer", "lookup-second-question", seed, session, caller, record.delegation_id):
        assert marker in wire, marker
    # 同次回答可能有block/message多个item，before只排除锚点及之后的投影。
    source_items = case.container.checkpointer._storage.storage.read_items(session, caller)
    included_answer = any(row.item_id in {entry[0] for entry in expected} and "lookup-second-answer" in _wire_text(row.payload) for row in source_items)
    assert ("lookup-second-answer" in wire) == included_answer
    for marker in ("lookup-third-", "lookup-revised-", "lookup-replaced-", "lookup-inflight-", "call_lookup_", "lookup-parent-dispatched-", "lookup-caller-later-"):
        assert marker not in wire, marker
    return {"selection": selection, "caller": caller, "child": child, "capture": point.model_dump(),
            "expected_members": expected, "inherited_members": inherited, "request": case.request(child_call), "reference": reference}


async def test_model_task_selects_nonancestor_explicit_and_exact_thread_prefixes(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry, branched_item_history: BranchedItemHistory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, history = source_case, branched_item_history
    session, main, item = history.session, history.thread, history.item_id
    # replay的parent_view可能保留整条来源链；完整fork的独立根才证明非祖先定位。
    full_point = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"thread_id": main, "mode": "full_rollout_copy"})
    assert full_point.status_code == 200, full_point.text
    point = full_point.json()["data"]
    full_deadline = deadlines.for_operation(target=main, operation_key="lookup-independent-root")
    copied = await full_deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": "lookup-independent-root", "mode": "full_rollout_copy",
        "source_uri": point["source_uri"], "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "target_container_uri": point["child_container_uri"],
    }), "完整fork生成独立根")
    assert copied.status_code == 200, copied.text
    full_deadline.complete()
    main = copied.json()["data"]["thread_id"]
    independent = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"thread_id": main, "item_id": item})
    assert independent.status_code == 200, independent.text
    history = replace(history, thread=main, latest=independent.json()["data"], ancestors=case.sql(session, main, LINEAGE_QUERY))
    assert history.latest["source_view_id"] not in {row[0] for row in history.ancestors}
    # 先由公开前缀入口建立含相同历史ID的独立owner，再产生caller自己的新view。
    deadline = deadlines.for_operation(target=main, operation_key="lookup-caller-fork")
    response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": "lookup-caller-fork", "mode": "view_prefix_fork", "anchor_item_id": item,
        "source_uri": history.latest["source_uri"], "target_container_uri": history.latest["child_container_uri"],
    }), "创建相同历史ID的独立caller")
    assert response.status_code == 200, response.text
    deadline.complete()
    caller = response.json()["data"]["thread_id"]
    await case.plan(session, [{"content": "lookup-caller-later-answer"}], thread_id=caller)
    accepted = await _send_thread(case, session, caller, "lookup-caller-later-question")
    await _job(case, accepted["job_id"])
    caller_point = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"thread_id": caller, "item_id": item})
    assert caller_point.status_code == 200, caller_point.text
    caller_view = caller_point.json()["data"]["source_view_id"]
    assert caller_view != history.latest["source_view_id"]
    assert case.sql(session, main, "SELECT view_id FROM context_views WHERE view_id=?", (caller_view,)) == []
    for thread in (main, caller):
        assert case.sql(session, thread, "SELECT item_id FROM item_catalog WHERE item_id=?", (item,)) == [(item,)]

    source = case.container.checkpointer.forks.source
    original_capture = source._capture_fork_source_snapshot
    committed = []
    observed_candidates = {}

    async def capture_after_committed_task(**kwargs):
        caller_thread = kwargs["source_owner"].thread_id
        with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
            record = owner.control_store.get_thread_creation_record(kwargs["capture_record"].creation_key)
        call_id = json.loads(record.task_seed)["parent_tool_call_id"]
        rows = case.container.checkpointer._storage.storage.read_items(session, caller_thread)
        calls = [row for row in rows if row.semantic_kind == "tool_call" and call_id in _wire_text(row.payload)]
        assert calls, "真实task canonical调用必须先于定位/capture提交"
        for row in calls:
            assert case.sql(session, caller_thread, "SELECT 1 FROM item_catalog WHERE item_id=?", (row.item_id,)) == [(1,)]
        committed.append({"thread": caller_thread, "items": [row.item_id for row in calls]})
        candidates = case.sql(session, caller_thread,
            "SELECT view.view_id FROM context_views view JOIN context_view_items member USING(view_id) "
            "WHERE member.item_id=? AND member.visible=1 AND member.context_selected=1 "
            "AND view.view_kind<>'checkpoint_messages' ORDER BY view.creation_sequence DESC", (item,))
        assert candidates
        observed_candidates[call_id] = candidates[0][0]
        return await original_capture(**kwargs)

    async def no_preview(**_kwargs):
        pytest.fail("task item定位必须直接在capture保护内完成")

    monkeypatch.setattr(source, "_capture_fork_source_snapshot", capture_after_committed_task)
    monkeypatch.setattr(source, "preflight_fork", no_preview)
    evidence = []
    for thread, view, explicit, before, label in (
        (main, history.latest["source_view_id"], False, False, "nonancestor"),
        (main, history.old_view, True, True, "explicit"),
        (caller, caller_view, False, False, "caller"),
    ):
        evidence.append(await _model_task_prefix(case, deadlines, history, caller=thread, expected_view=view,
                                               explicit=explicit, before=before, label=label, observed_candidates=observed_candidates))
    assert [row["thread"] for row in committed] == [main, main, caller]
    case.write_actual("task-item-lookup", {"history": {"item": item, "old_view": history.old_view, "latest": history.latest,
                      "active_lineage": history.ancestors}, "committed_calls": committed, "tasks": evidence})


async def test_model_task_missing_item_does_not_publish_a_child(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session, thread = await _completed_parent(case)
    await case.plan(session, [_tool_reply("task", {"description": "不能用其它item代替缺失来源", "context": {
        "mode": "view_prefix_fork", "item_id": "missing-task-item",
    }}, "call_missing_task_item")])
    deadline = deadlines.for_operation(target=thread, operation_key="model-task-missing")
    accepted = await _send_thread(case, session, thread, "尝试不存在的item")
    job = await deadline.wait(_job(case, accepted["job_id"], expected="failed"), "无候选task明确失败")
    deadline.complete()
    assert "source-mismatch" in job["error_message"]
    children = await case.client.get(f"/api/v1/sessions/{session}/child-threads")
    assert children.status_code == 200 and children.json()["data"]["items"] == [], children.text
    with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
        assert not owner.control_store.list_child_thread_rows()
        assert not owner.control_store.list_initial_execution_intents()
    case.write_actual("task-item-missing", {"job": job, "children": children.json()})


async def test_model_task_rejects_all_source_overrides_before_delegation(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session, thread = await _completed_parent(case)
    item = _first_response_item(case, session, thread)
    calls = [_tool_reply("task", {"description": "不得覆盖真实执行来源", "context": {
        "mode": "view_prefix_fork", "item_id": item, field: "another-owner",
    }}, "call_override_" + field)["tool_calls"][0] for field in ("thread_id", "session_id", "source_uri")]
    _, result_call = await case.plan(session, [{"tool_calls": calls}, {"content": "已拒绝全部非法来源"}])
    deadline = deadlines.for_operation(target=thread, operation_key="model-task-source-overrides")
    accepted = await _send_thread(case, session, thread, "检查来源覆盖拒绝")
    await deadline.wait(_job(case, accepted["job_id"]), "非法来源返回配对错误结果")
    deadline.complete()
    results = [message for message in case.request(result_call)["messages"] if message.get("role") == "tool"]
    assert {message["tool_call_id"] for message in results} == {call["id"] for call in calls}
    for message in results:
        assert "extra" in _wire_text(message["content"]).lower()
    with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
        assert not owner.control_store.list_child_thread_rows()
        assert not owner.control_store.list_initial_execution_intents()
        assert owner.control_store.connection.execute("SELECT COUNT(*) FROM thread_creation_records").fetchone()[0] == 0
    case.write_actual("task-source-overrides", {"calls": calls, "results": results})
