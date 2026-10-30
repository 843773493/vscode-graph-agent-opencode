"""main 回退与摘要只改变自己的有效视图，child 历史与续聊保持隔离。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.message import MessageReplayAccepted, MessageRunAccepted
from app.schemas.internal_v2.session import (
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from tests.integration.backend.sessions.attachments.test_thread_isolation import (
    history,
    state,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - 复用真实进程与 model_stream，工作区归当前测试
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


async def complete(case: CommunicationCase, session: str, thread: str, text: str, reply: str) -> MessageRunAccepted:
    await case.plan(session, [{"content": reply}], thread_id=thread)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{thread}/messages", json={
        "message": {"content": text}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    accepted = MessageRunAccepted.model_validate(response.json()["data"])
    assert accepted.thread_id == accepted.dispatch.thread_id == thread
    await _completed(case.client, accepted.job_id)
    return accepted


async def fork_child(case: CommunicationCase, session: str, key: str) -> SessionForkResultDTO:
    point_response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point")
    assert point_response.status_code == 200, point_response.text
    point = SessionForkSourcePointDTO.model_validate(point_response.json()["data"])
    response = await case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": key, "source_uri": point.source_uri,
        "source_view_id": point.source_view_id, "source_view_revision": point.source_view_revision,
        "anchor_item_id": point.anchor_item_id, "target_container_uri": point.child_container_uri,
        "mode": "view_prefix_fork", "creation_kind": "new_child_thread",
    })
    assert response.status_code == 200, response.text
    result = SessionForkResultDTO.model_validate(response.json()["data"])
    assert result.session.session_id == session
    return result


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True)
async def test_main_regenerate_preserves_child_storage_request_and_restart(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "main 回退与 child 独立恢复")
    main_response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main_response.status_code == 200, main_response.text
    main = main_response.json()["data"]["thread_id"]
    initial = await complete(case, session, main, "共同的冻结输入", "共同的冻结回答")
    forked = await fork_child(case, session, "rewind-isolated-child")
    child = forked.thread_id
    assert forked.session.session_id == session and child != main
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    roots = {thread: resolver.resolve_thread_node(session, thread) / "rollout" for thread in (main, child)}
    assert roots[main] != roots[child]

    target = await complete(case, session, main, "main 专有的待重生成输入", "main 将被隐藏的旧回答")
    main_before_child = state(roots[main])
    child_input = await complete(case, session, child, "child 专有的独立输入", "child 专有的独立回答")
    assert state(roots[main]) == main_before_child
    child_before = state(roots[child])
    child_history = await history(case, session, child)
    assert [turn.final_response for turn in child_history.items] == ["共同的冻结回答", "child 专有的独立回答"]
    assert [turn.turn_id for turn in child_history.items] == [initial.turn_id, child_input.turn_id]

    replay_call, = await case.plan(session, [{"content": "main 重生成后的新回答"}], thread_id=main)
    replay_response = await case.client.post(f"/api/v1/sessions/{session}/turns/{target.turn_id}/replay", json={
        "action": "regenerate", "acknowledge_context_only": True,
    })
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.headers["X-Request-ID"] == replay_response.json()["request_id"]
    replayed = MessageReplayAccepted.model_validate(replay_response.json()["data"])
    assert replayed.thread_id == main and replayed.replaced_message_id == target.message_id
    assert replayed.turn_id != target.turn_id and replayed.message_id != target.message_id
    await _completed(case.client, replayed.job_id)
    assert state(roots[child]) == child_before
    assert await history(case, session, child) == child_history
    assert (roots[main] / "rollout.jsonl").read_bytes().startswith(main_before_child.canonical)
    main_history = await history(case, session, main)
    assert [turn.final_response for turn in main_history.items] == ["共同的冻结回答", "main 重生成后的新回答"]
    assert [turn.turn_id for turn in main_history.items] == [initial.turn_id, replayed.turn_id]
    replay_messages = json.dumps(case.request(replay_call)["messages"], ensure_ascii=False)
    assert "共同的冻结输入" in replay_messages and "共同的冻结回答" in replay_messages
    assert replay_messages.count("main 专有的待重生成输入") == 1
    assert all(text not in replay_messages for text in (
        "main 将被隐藏的旧回答", "child 专有的独立输入", "child 专有的独立回答",
    ))

    finished = {thread: state(roots[thread]) for thread in (main, child)}
    pages = {main: main_history, child: child_history}
    await case.hard_restart()
    for thread in (main, child):
        assert state(roots[thread]) == finished[thread]
        assert await history(case, session, thread) == pages[thread]

    child_call, = await case.plan(session, [{"content": "child 冷恢复后继续完成"}], thread_id=child)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json={
        "message": {"content": "child 冷恢复后的后继输入"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    successor = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, successor.job_id)
    assert state(roots[main]) == finished[main]
    assert await history(case, session, main) == main_history
    child_messages = json.dumps(case.request(child_call)["messages"], ensure_ascii=False)
    assert all(text in child_messages for text in (
        "共同的冻结输入", "共同的冻结回答", "child 专有的独立输入",
        "child 专有的独立回答", "child 冷恢复后的后继输入",
    ))
    assert all(text not in child_messages for text in (
        "main 专有的待重生成输入", "main 将被隐藏的旧回答", "main 重生成后的新回答",
    ))
    assert (roots[child] / "rollout.jsonl").read_bytes().startswith(child_before.canonical)
    captures = {call: json.loads((case.replay.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    logs = list((resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"))
    proofs = {thread: audit_sealed_requests(roots[thread] / "index.sqlite", logs, captures)
              for thread in (main, child)}
    assert len(case.planned_calls) == 5
    case.record("thread-rewind-isolation", {
        "session_id": session, "main_thread_id": main, "child_thread_id": child,
        "initial": initial.model_dump(mode="json"), "replaced": target.model_dump(mode="json"),
        "replayed": replayed.model_dump(mode="json"), "child_input": child_input.model_dump(mode="json"),
        "child_successor": successor.model_dump(mode="json"), "request_audit": proofs,
        "scope": "真实 Gateway/Backend、消息/fork/main regenerate、完整 SQLite/JSONL、硬重启及 child 续聊；不代替 compaction、Web、Windows 或全部 8.3",
    })


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True)
async def test_main_compaction_preserves_child_storage_request_and_restart(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "main 摘要与 child 独立恢复")
    main_response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main_response.status_code == 200, main_response.text
    main = main_response.json()["data"]["thread_id"]
    # 六轮完整长回答使中段足够长，保留近期消息后仍可真正缩短正文。
    shared = [(f"compaction-shared-question-{index}", f"compaction-shared-answer-{index}:" + "已完成的详细记录。" * 120)
              for index in range(6)]
    initial = [await complete(case, session, main, text, reply) for text, reply in shared]
    forked = await fork_child(case, session, "compaction-isolated-child")
    child = forked.thread_id
    assert child != main
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    roots = {thread: resolver.resolve_thread_node(session, thread) / "rollout" for thread in (main, child)}
    main_before = state(roots[main])
    child_input = await complete(case, session, child, "child 未摘要的独立输入", "child 未摘要的独立回答")
    assert state(roots[main]) == main_before
    child_before = state(roots[child])
    child_history = await history(case, session, child)
    assert [turn.turn_id for turn in child_history.items] == [item.turn_id for item in (*initial, child_input)]

    scheduled = await case.client.post(f"/api/v1/sessions/{session}/compact")
    assert scheduled.status_code == 200 and scheduled.json()["data"]["status"] == "scheduled", scheduled.text
    assert state(roots[child]) == child_before
    summary = "main 专有摘要：已经完成中段工作，继续处理保留的近期记录。"
    summary_call, final_call = await case.plan(session, [
        {"content": summary}, {"content": "main 摘要后专有的回答"},
    ], thread_id=main)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{main}/messages", json={
        "message": {"content": "main 专有的摘要后输入"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    compacted = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, compacted.job_id)
    with sqlite3.connect((roots[main] / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        summaries = connection.execute("SELECT item_id FROM item_catalog WHERE semantic_kind='compaction_summary'").fetchall()
    assert len(summaries) == 1
    candidate = json.dumps(case.request(summary_call)["messages"], ensure_ascii=False)
    effective = json.dumps(case.request(final_call)["messages"], ensure_ascii=False)
    assert summary not in candidate and effective.count(summary) == 1
    assert any(reply in candidate and reply not in effective for _, reply in shared)
    assert "main 专有的摘要后输入" in effective
    assert "child 未摘要的独立输入" not in candidate + effective
    assert "child 未摘要的独立回答" not in candidate + effective
    assert state(roots[child]) == child_before
    assert await history(case, session, child) == child_history
    assert (roots[main] / "rollout.jsonl").read_bytes().startswith(main_before.canonical)

    finished = {thread: state(roots[thread]) for thread in (main, child)}
    pages = {thread: await history(case, session, thread) for thread in (main, child)}
    await case.hard_restart()
    for thread in (main, child):
        assert state(roots[thread]) == finished[thread]
        assert await history(case, session, thread) == pages[thread]
    child_call, = await case.plan(session, [{"content": "child 未摘要历史冷恢复后完成"}], thread_id=child)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json={
        "message": {"content": "child 未摘要历史冷恢复后的后继输入"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    successor = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, successor.job_id)
    assert state(roots[main]) == finished[main]
    assert await history(case, session, main) == pages[main]
    wire = json.dumps(case.request(child_call)["messages"], ensure_ascii=False)
    assert all(text in wire and reply in wire for text, reply in shared)
    assert all(text in wire for text in ("child 未摘要的独立输入", "child 未摘要的独立回答", "child 未摘要历史冷恢复后的后继输入"))
    assert all(text not in wire for text in (summary, "main 专有的摘要后输入", "main 摘要后专有的回答"))
    assert (roots[child] / "rollout.jsonl").read_bytes().startswith(child_before.canonical)
    captures = {call: json.loads((case.replay.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    logs = list((resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"))
    proofs = {thread: audit_sealed_requests(roots[thread] / "index.sqlite", logs, captures)
              for thread in (main, child)}
    assert len(case.planned_calls) == 10
    case.record("thread-compaction-isolation", {
        "session_id": session, "main_thread_id": main, "child_thread_id": child,
        "initial": [item.model_dump(mode="json") for item in initial],
        "compacted": compacted.model_dump(mode="json"), "summary_item_ids": summaries,
        "child_input": child_input.model_dump(mode="json"), "child_successor": successor.model_dump(mode="json"),
        "request_audit": proofs,
        "scope": "真实 Gateway/Backend、消息/fork/手动摘要、完整 SQLite/JSONL、硬重启及未摘要 child 续聊；不代替 Web、Windows 或全部 8.3",
    })
