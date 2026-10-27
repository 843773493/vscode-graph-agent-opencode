"""通过消息和回放入口生成分支，验证 item-only 来源预览。"""

from __future__ import annotations

import pytest

from tests.integration.backend.sessions.fork.test_task_context import (
    deadlines as deadlines,  # noqa: PLC0414 - 复用固定60秒fork监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实 Gateway、runtime 和 model_stream
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry


@pytest.mark.parametrize("source_case", [{
    "managed_gateway": True, "terminal": True, "browser": True, "workspace_template": "model_tool_test_workspace",
}], indirect=True)
async def test_item_only_source_point_finds_latest_nonancestor_branch(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session = await create_session(case.client, "全分支 item 定位")
    await case.plan(session, [{"content": content} for content in (
        "lookup-first-answer", "lookup-second-answer", "lookup-third-answer", "lookup-revised-third", "lookup-replaced-second",
    )])
    first = await _send(case, session, "lookup-first-question")
    first_job = await _job(case, first["job_id"])
    thread = first_job["thread_id"]
    second = await _send(case, session, "lookup-second-question")
    await _job(case, second["job_id"])
    third = await _send(case, session, "lookup-third-question")
    await _job(case, third["job_id"])
    (item_id,) = case.sql(session, thread,
        "SELECT turn.final_item_id FROM turn_acceptances acceptance "
        "JOIN turn_records turn USING(turn_id) WHERE acceptance.accepted_ingress_id=?", (second["message_id"],))[0]
    endpoint = f"/api/v1/sessions/{session}/fork-source-point"
    params = {"thread_id": thread, "item_id": item_id}
    old = await case.client.get(endpoint, params=params)
    assert old.status_code == 200, old.text
    old_view = old.json()["data"]["source_view_id"]

    # 第三轮改写产生 B，保留第二轮；随后第二轮改写产生 C，移除原第二轮。
    for original, content in ((third, "lookup-revised-question"), (second, "lookup-replaced-question")):
        replay = await case.client.post(f"/api/v1/sessions/{session}/messages/{original['message_id']}/replay", json={
            "action": "edit_and_continue", "content": content, "acknowledge_context_only": True,
        })
        assert replay.status_code == 200, replay.text
        await _job(case, replay.json()["data"]["job_id"])
        point = await case.client.get(endpoint, params=params)
        assert point.status_code == 200, point.text
        if original is third:
            latest = point.json()["data"]
            assert latest["source_view_id"] != old_view
        else:
            assert point.json()["data"]["source_view_id"] == latest["source_view_id"]
            assert point.json()["data"]["anchor_item_id"] == item_id

    lineage_query = (
        "WITH RECURSIVE lineage(view_id) AS (SELECT branch.head_view_id "
        "FROM checkpoint_namespace_state namespace JOIN branches branch "
        "ON branch.branch_id=namespace.active_branch_id WHERE namespace.checkpoint_ns='' UNION "
        "SELECT view.parent_view_id FROM context_views view JOIN lineage ON view.view_id=lineage.view_id "
        "WHERE view.parent_view_id IS NOT NULL) SELECT view_id FROM lineage"
    )
    ancestors = case.sql(session, thread, lineage_query)
    assert case.sql(session, thread,
        "SELECT member.item_id FROM checkpoint_namespace_state namespace "
        "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
        "JOIN context_view_items member ON member.view_id=branch.head_view_id "
        "WHERE namespace.checkpoint_ns='' AND member.item_id=? AND member.visible=1 AND member.context_selected=1",
        (item_id,)) == []
    explicit = await case.client.get(endpoint, params={**params, "source_view_id": old_view})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["data"]["source_view_id"] == old_view
    missing = await case.client.get(endpoint, params={**params, "item_id": "missing-item"})
    assert missing.status_code == 400, missing.text

    # 完整副本建立独立 active root；历史 checkpoint 仍标识原 namespace，不能丢失旧分支。
    full = await case.client.get(endpoint, params={"thread_id": thread, "mode": "full_rollout_copy"})
    assert full.status_code == 200, full.text
    point = full.json()["data"]
    deadline = deadlines.for_operation(target=point["child_container_uri"], operation_key="item-lookup-full-copy")
    copied = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": "item-lookup-full-copy", "mode": "full_rollout_copy",
        "source_uri": point["source_uri"], "source_view_id": point["source_view_id"],
        "source_view_revision": point["source_view_revision"],
        "target_container_uri": point["child_container_uri"],
    }), "公开完整fork")
    assert copied.status_code == 200, copied.text
    deadline.complete()
    child = copied.json()["data"]["thread_id"]
    child_ancestors = case.sql(session, child, lineage_query)
    assert latest["source_view_id"] not in {row[0] for row in child_ancestors}
    child_point = await case.client.get(endpoint, params={**params, "thread_id": child})
    assert child_point.status_code == 200, child_point.text
    assert child_point.json()["data"]["source_view_id"] == latest["source_view_id"]
    child_explicit = await case.client.get(endpoint, params={
        **params, "thread_id": child, "source_view_id": old_view,
    })
    assert child_explicit.status_code == 200, child_explicit.text
    assert child_explicit.json()["data"]["source_view_id"] == old_view
    case.write_actual("item-only-nonancestor-source", {
        "session_id": session, "thread_id": thread, "item_id": item_id,
        "old_view": old_view, "latest": latest, "active_lineage": ancestors,
        "explicit": explicit.json()["data"], "missing_status": missing.status_code,
        "full_copy_thread": child, "full_copy_lineage": child_ancestors,
        "full_copy_historical_point": child_point.json()["data"],
    })
