"""child 通过模型工具摘要及读回归档，main 存储与请求保持隔离。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.turn import TurnHistoryPageDTO
from tests.integration.backend.sessions.attachments.test_thread_isolation import (
    history,
    state,
)
from tests.integration.backend.sessions.fork.test_thread_rewind import (
    complete,
    fork_child,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - 复用真实进程与唯一模型协议装具
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True)
async def test_child_compaction_tool_preserves_main_and_restores_own_archive(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    # 通过正常 catalog/selection 启用工具，不替测试预置 graph、view 或摘要。
    catalog = await case.client.get("/api/v1/tools", params={"agent_id": "default"})
    assert catalog.status_code == 200, catalog.text
    tools = {tool["name"]: tool for tool in catalog.json()["data"]}
    selected = await case.client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {"tool_id": tools[name]["tool_id"], "execution_enabled": True, "model_visible": True}
            for name in ("compact_conversation", "read_compaction_history")
        ],
    })
    assert selected.status_code == 200, selected.text
    session = await create_session(case.client, "child 摘要工具与 main 隔离")
    main_response = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main_response.status_code == 200, main_response.text
    main = main_response.json()["data"]["thread_id"]
    shared = [(f"child-compact-shared-question-{index}", f"child-compact-shared-answer-{index}:" + "已完成的详细记录。" * 120)
              for index in range(6)]
    initial = [await complete(case, session, main, question, answer) for question, answer in shared]
    child = (await fork_child(case, session, "child-compact-isolation")).thread_id
    assert child != main
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    roots = {thread: resolver.resolve_thread_node(session, thread) / "rollout" for thread in (main, child)}
    archives = {thread: resolver.resolve_session_node(session) / "context" / thread / "history.md"
                for thread in (main, child)}
    assert not any(path.exists() for path in archives.values())
    child_before = state(roots[child])
    main_private = await complete(case, session, main, "main-private-question", "main-private-answer")
    assert state(roots[child]) == child_before
    main_before = state(roots[main])
    main_history = await history(case, session, main)

    summary = "child-only-summary: 已完成共享历史的中段，继续保留近期记录。"
    compact_call, summary_call, archive_call, final_call = await case.plan(session, [
        _tool("compact_conversation", {}, "child-compact"),
        {"content": summary},
        _tool("read_compaction_history", {"line_offset": 1, "limit": 2000}, "child-read-archive"),
        {"content": "child-only-compacted-answer"},
    ], thread_id=child, usage={"prompt_tokens": 100000, "completion_tokens": 16, "total_tokens": 100016})
    sent = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json={
        "message": {"content": "child-only-compaction-question"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert sent.status_code == 200, sent.text
    compacted = MessageRunAccepted.model_validate(sent.json()["data"])
    assert compacted.thread_id == compacted.dispatch.thread_id == child
    await _completed(case.client, compacted.job_id)
    compact_result = case.tool_result(session, compact_call + ":tool-call:child-compact")
    assert "compaction scheduled" in json.dumps(compact_result)
    with sqlite3.connect((roots[child] / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        summaries = connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind='compaction_summary'",
        ).fetchall()
    assert len(summaries) == 1
    before = json.dumps(case.request(compact_call)["messages"], ensure_ascii=False)
    candidate = json.dumps(case.request(summary_call)["messages"], ensure_ascii=False)
    effective = json.dumps(case.request(archive_call)["messages"], ensure_ascii=False)
    assert summary not in candidate and effective.count(summary) == 1
    assert "child-only-compaction-question" in effective
    assert all(question in before and answer in before for question, answer in shared)
    removed = [(question, answer) for question, answer in shared if answer in candidate and answer not in effective]
    assert removed, "工具必须实际移出中段原文，不能只返回调度成功"
    archive_payload = case.tool_result(session, archive_call + ":tool-call:child-read-archive")
    archive_result = json.dumps(archive_payload, ensure_ascii=False)
    assert all(question in archive_result and answer in archive_result for question, answer in removed)
    assert "read_compaction_history(line_offset=1, limit=200)" in effective
    assert all(text not in before + candidate + effective + archive_result for text in (
        "main-private-question", "main-private-answer",
    ))
    wire_tools = case.request(compact_call)["tools"]
    assert isinstance(wire_tools, list)
    assert all(tool in {entry["function"]["name"] for entry in wire_tools}
               for tool in ("compact_conversation", "read_compaction_history"))
    final_wire = json.dumps(case.request(final_call)["messages"], ensure_ascii=False)
    assert all(question in final_wire and answer in final_wire for question, answer in removed)
    assert state(roots[main]) == main_before
    assert await history(case, session, main) == main_history
    assert not archives[main].exists()
    archived = archives[child].read_bytes()
    assert (roots[child] / "rollout.jsonl").read_bytes().startswith(child_before.canonical)
    child_history = await history(case, session, child)
    assert [turn.turn_id for turn in child_history.items] == [accepted.turn_id for accepted in (*initial, compacted)]
    # 默认 history 不包含活动正文；显式请求正式详情，验证唯一摘要及工具配对。
    detail_request = {"direction": "head", "turns": 10, "include": [
        "user", "text", "reasoning_detail", "tool_call", "tool_result", "final_response", "internal",
    ]}
    details = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/history", json=detail_request)
    assert details.status_code == 200, details.text
    full_history = TurnHistoryPageDTO.model_validate(details.json()["data"])
    compact_turn = full_history.items[-1]
    summary_part, = [part for part in compact_turn.response_parts if part.kind == "compaction_summary"]
    assert summary_part.source.item_id == summaries[0][0]
    tool_calls = [part for part in compact_turn.response_parts if part.kind == "tool_call"]
    tool_results = [part for part in compact_turn.response_parts if part.kind == "tool_result"]
    assert [part.tool_name for part in tool_calls] == ["compact_conversation", "read_compaction_history"]
    assert [part.tool_call_id for part in tool_results] == [part.tool_call_id for part in tool_calls]
    assert compact_turn.final_response == "child-only-compacted-answer"

    finished = {thread: state(roots[thread]) for thread in (main, child)}
    await case.hard_restart()
    for thread, page in ((main, main_history), (child, child_history)):
        assert state(roots[thread]) == finished[thread]
        assert await history(case, session, thread) == page
    cold_details = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/history", json=detail_request)
    assert cold_details.status_code == 200, cold_details.text
    assert TurnHistoryPageDTO.model_validate(cold_details.json()["data"]) == full_history
    assert archives[child].read_bytes() == archived and not archives[main].exists()

    reread_call, successor_call = await case.plan(session, [
        _tool("read_compaction_history", {"line_offset": 1, "limit": 2000}, "child-reread-archive"),
        {"content": "child-cold-successor-answer"},
    ], thread_id=child)
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{child}/messages", json={
        "message": {"content": "child-cold-successor-question"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    successor = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, successor.job_id)
    restored_archive = case.tool_result(session, reread_call + ":tool-call:child-reread-archive")
    assert restored_archive["content"] == archive_payload["content"]
    assert archives[child].read_bytes() == archived and not archives[main].exists()
    assert state(roots[main]) == finished[main]
    assert await history(case, session, main) == main_history
    child_messages = case.request(reread_call)["messages"]
    assert isinstance(child_messages, list)
    child_wire = json.dumps(child_messages, ensure_ascii=False)
    assert all(text in child_wire for text in (summary, "child-only-compaction-question", "child-only-compacted-answer"))
    assert all(text not in child_wire for text in ("main-private-question", "main-private-answer"))
    # 模型主动读回的 tool 正文可以含归档原文；被移出的旧对话消息不能复活。
    direct_context = json.dumps([message for message in child_messages if message["role"] != "tool"], ensure_ascii=False)
    assert all(question not in direct_context and answer not in direct_context for question, answer in removed)
    assert all(question in json.dumps(case.request(successor_call)["messages"], ensure_ascii=False)
               for question, _ in removed)
    child_finished = state(roots[child])
    child_page = await history(case, session, child)
    planned_before = set(case.planned_calls)
    main_successor = await complete(case, session, main, "main-cold-successor-question", "main-cold-successor-answer")
    main_call, = case.planned_calls.keys() - planned_before
    assert state(roots[child]) == child_finished
    assert await history(case, session, child) == child_page
    main_wire = json.dumps(case.request(main_call)["messages"], ensure_ascii=False)
    assert all(question in main_wire and answer in main_wire for question, answer in shared)
    assert "main-private-question" in main_wire and "main-private-answer" in main_wire
    assert all(text not in main_wire for text in (
        summary, "child-only-compaction-question", "child-only-compacted-answer",
        "child-cold-successor-question", "child-cold-successor-answer",
    ))
    captures = {call: json.loads((case.replay.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    logs = list((resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"))
    proofs = {thread: audit_sealed_requests(roots[thread] / "index.sqlite", logs, captures) for thread in (main, child)}
    assert len(case.planned_calls) == 14
    case.record("child-compaction-isolation", {
        "session_id": session, "main_thread_id": main, "child_thread_id": child,
        "compacted": compacted.model_dump(mode="json"), "summary_item_ids": summaries,
        "main_private": main_private.model_dump(mode="json"), "child_successor": successor.model_dump(mode="json"),
        "main_successor": main_successor.model_dump(mode="json"), "removed_questions": [question for question, _ in removed],
        "archive_tool_result": archive_payload, "request_audit": proofs,
        "scope": "真实 Gateway/Backend、消息/fork/child 摘要工具/归档读取、完整 SQLite/JSONL、硬重启及双方续聊；不代替 Web、Windows 或全部 8.3",
    })
