"""通过真实压缩及公开 fork 验证摘要、保留尾部与展示顺序。"""

from __future__ import annotations

import pytest

from tests.integration.backend.sessions.fork.test_task_context import (
    _send_thread,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    deadlines as deadlines,  # noqa: PLC0414 - 保留正式60秒fork监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实服务及model_stream
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry


@pytest.mark.parametrize("source_case", [{
    "managed_gateway": True, "terminal": True, "browser": True,
}], indirect=True)
async def test_compacted_prefix_keeps_effective_and_display_orders(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session = await create_session(case.client, "真实摘要前缀")
    # 默认保留六条近期消息及稳定前缀；六轮确保中段含完整长回答，摘要确实缩短正文。
    history_count = 6
    history = [{"content": f"compact-prefix-answer-{index}:" + "已经完成的详细工作记录。" * 120}
               for index in range(history_count)]
    await case.plan(session, history, usage={"prompt_tokens": 89999, "completion_tokens": 1, "total_tokens": 90000})
    for index in range(history_count):
        accepted = await _send(case, session, f"compact-prefix-question-{index}")
        thread = (await _job(case, accepted["job_id"]))["thread_id"]
    assert case.sql(session, thread, "SELECT item_id FROM item_catalog WHERE semantic_kind='compaction_summary'") == []
    compact = await case.client.post(f"/api/v1/sessions/{session}/compact")
    assert compact.status_code == 200 and compact.json()["data"]["status"] == "scheduled", compact.text

    # 调度成功还没有摘要；下一次真实执行生成候选、提交压缩并继续模型请求。
    summary_text = "compact-prefix-summary: 已完成中段工作，继续处理保留的近期记录。"
    summary_call, final_call = await case.plan(session, [
        {"content": summary_text}, {"content": "compact-prefix-final-answer"},
    ])
    accepted = await _send(case, session, "compact-prefix-current-question")
    await _job(case, accepted["job_id"])
    summaries = case.sql(session, thread, "SELECT item_id FROM item_catalog WHERE semantic_kind='compaction_summary'")
    assert len(summaries) == 1
    summary_id, = summaries[0]
    candidate_wire = _wire_text(case.request(summary_call)["messages"])
    final_wire = _wire_text(case.request(final_call)["messages"])
    assert summary_text not in candidate_wire and final_wire.count(summary_text) == 1
    assert "compact-prefix-current-question" in final_wire
    source_items = case.container.checkpointer._storage.storage.read_items(session, thread)
    markers = {item.item_id: marker for item in source_items for marker in (
        *(f"compact-prefix-{kind}-{index}" for index in range(history_count) for kind in ("question", "answer")),
        "compact-prefix-current-question", "compact-prefix-final-answer", summary_text,
    ) if marker in _wire_text(item.payload)}
    # 同一次响应的block与message投影有不同item；可见资格由view决定，不能按正文猜身份。
    assert len(set(markers.values())) == 2 * history_count + 3
    final_id, = case.sql(session, thread,
        "SELECT turn.final_item_id FROM turn_acceptances acceptance JOIN turn_records turn USING(turn_id) "
        "WHERE acceptance.accepted_ingress_id=?", (accepted["message_id"],))[0]
    observed = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
        "thread_id": thread, "item_id": final_id,
    })
    assert observed.status_code == 200, observed.text
    point = observed.json()["data"]
    members = case.sql(session, thread,
        "SELECT member.item_id,member.logical_item_ordinal,member.context_ordinal,item.item_sequence "
        "FROM context_view_items member JOIN item_catalog item USING(item_id) "
        "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal",
        (point["source_view_id"],))
    summary_member, = [row for row in members if row[0] == summary_id]
    # 摘要后仍有物理上更早的尾部正文，按append顺序截断会得到不同结果。
    assert any(row[2] > summary_member[2] and row[3] < summary_member[3] for row in members)
    assert [row[0] for row in members] != [row[0] for row in sorted(members, key=lambda row: row[3])]
    evidence = []
    for anchor_id, explicit_view in ((summary_id, False), (final_id, True)):
        for anchor_mode in ("inclusive", "before"):
            key = f"compacted-prefix-{anchor_id}-{anchor_mode}"
            body = {
                "idempotency_key": key, "mode": "view_prefix_fork", "source_uri": point["source_uri"],
                "target_container_uri": point["child_container_uri"],
                "anchor_item_id": anchor_id, "anchor_mode": anchor_mode,
            }
            if explicit_view:
                body["source_view_id"] = point["source_view_id"]
                body["source_view_revision"] = point["source_view_revision"]
            deadline = deadlines.for_operation(target=point["child_container_uri"], operation_key=key)
            response = await deadline.wait(case.client.post(
                f"/api/v1/sessions/{session}/fork-context", json=body,
            ), "发布真实摘要前缀")
            assert response.status_code == 200, response.text
            deadline.complete()
            result = response.json()["data"]
            anchor, = [row for row in members if row[0] == anchor_id]
            selected = [row for row in members if row[2] < anchor[2] or (row[2] == anchor[2] and anchor_mode == "inclusive")]
            assert selected
            target = result["thread_id"]
            selected_query = (
                "SELECT member.item_id,member.logical_item_ordinal,member.context_ordinal FROM checkpoint_namespace_state namespace "
                "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
                "JOIN context_view_items member ON member.view_id=branch.head_view_id "
                "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 "
            )
            context_ids = case.sql(session, target, selected_query + "ORDER BY member.context_ordinal")
            display_ids = case.sql(session, target, selected_query + "ORDER BY member.logical_item_ordinal")
            assert [row[0] for row in context_ids] == [row[0] for row in selected]
            assert [row[0] for row in display_ids] == [row[0] for row in sorted(selected, key=lambda row: row[1])]
            assert case.call_count(session, target) == 0
            call, = await case.plan(session, [{"content": "compacted-child-answer"}], thread_id=target)
            sent = await _send_thread(case, session, target, "compacted-child-question")
            await _job(case, sent["job_id"])
            wire = _wire_text(case.request(call)["messages"])
            selected_ids = {row[0] for row in selected}
            for marker in set(markers.values()):
                included = any(item_id in selected_ids for item_id, value in markers.items() if value == marker)
                assert (marker in wire) == included, (anchor_id, anchor_mode, marker)
            positions = [wire.index(markers[row[0]]) for row in selected if row[0] in markers]
            assert positions == sorted(positions)
            assert "compacted-child-question" in wire
            evidence.append({"result": result, "selected": selected, "context": context_ids, "display": display_ids, "call": call})
    case.write_actual("compacted-prefix", {"source_point": point, "source_members": members, "forks": evidence})
