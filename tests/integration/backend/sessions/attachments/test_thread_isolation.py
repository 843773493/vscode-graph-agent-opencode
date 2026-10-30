"""真实 Gateway 消息、fork 与 model_stream 验证并行 Thread 的持久隔离。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import parse_attachment_uri
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.session import (
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from app.schemas.internal_v2.turn import TurnHistoryPageDTO
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session

BODY = b"same bytes must remain independent uploads"


@dataclass(frozen=True)
class ThreadState:
    sql: tuple[str, ...]
    canonical: bytes


def state(root: Path) -> ThreadState:
    # 完整只读快照同时覆盖 offset、view、source revision、队列和执行索引。
    with sqlite3.connect((root / "index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        sql = tuple(connection.iterdump())
    return ThreadState(sql, (root / "rollout.jsonl").read_bytes())


async def send(case: CommunicationCase, session: str, thread: str, text: str) -> MessageRunAccepted:
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{thread}/messages", json={
        "message": {"content": text, "attachments": [upload(BODY).model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default", "delivery_policy": "after_turn"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    accepted = MessageRunAccepted.model_validate(response.json()["data"])
    assert accepted.thread_id == accepted.dispatch.thread_id == thread
    return accepted


async def history(case: CommunicationCase, session: str, thread: str) -> TurnHistoryPageDTO:
    response = await case.client.post(f"/api/v1/sessions/{session}/threads/{thread}/history", json={
        "direction": "head", "turns": 10,
    })
    assert response.status_code == 200, response.text
    page = TurnHistoryPageDTO.model_validate(response.json()["data"])
    assert page.thread_id == thread and not page.has_more
    return page


def associations(case: CommunicationCase) -> tuple[tuple[str, ...], ...]:
    database = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(connection.execute(
            "SELECT attachment_id,session_id,thread_id,binding_token,state "
            "FROM attachment_directory_associations ORDER BY attachment_id,thread_id,binding_token",
        ))


def directory_id(uri: str) -> str:
    identity = parse_attachment_uri(uri, require_file=True).attachment_id
    assert identity is not None
    return identity


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True)
async def test_parallel_threads_keep_fifo_context_and_upload_identity_through_restart(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "Thread 并行与目录隔离")
    await case.plan(session, [{"content": "共同历史已完成"}])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "共同的历史输入", "attachments": [upload(b"shared source").model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    initial = MessageRunAccepted.model_validate(response.json()["data"])
    await _completed(case.client, initial.job_id)
    point_response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point")
    assert point_response.status_code == 200, point_response.text
    point = SessionForkSourcePointDTO.model_validate(point_response.json()["data"])
    response = await case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": "parallel-thread-attachment-child", "source_uri": point.source_uri,
        "source_view_id": point.source_view_id, "source_view_revision": point.source_view_revision,
        "anchor_item_id": point.anchor_item_id, "target_container_uri": point.child_container_uri,
        "mode": "view_prefix_fork", "creation_kind": "new_child_thread",
    })
    assert response.status_code == 200, response.text
    child = SessionForkResultDTO.model_validate(response.json()["data"])
    assert child.session.session_id == session and child.thread_id != initial.thread_id
    threads = (initial.thread_id, child.thread_id)
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    roots = {thread: resolver.resolve_thread_node(session, thread) / "rollout" for thread in threads}
    assert roots[threads[0]] != roots[threads[1]]
    baseline = {thread: await history(case, session, thread) for thread in threads}
    original, = baseline[threads[0]].items[0].user_messages[0].attachments

    control = case.replay.control
    calls = {}
    texts = {thread: (f"{label}-head-private", f"{label}-queued-private")
             for thread, label in zip(threads, ("main", "child"), strict=True)}
    for thread in threads:
        calls[thread] = await case.plan(session, [
            _tool("read_file", {"path": original.file_id}, "read-common-source"),
            {"content": f"{thread} head finished"}, {"content": f"{thread} queue finished"},
        ], thread_id=thread)
        control.arm(calls[thread][0] + ":tool-call:read-common-source", "test.tool_result.committed")
        control.arm(calls[thread][2], "model.response_ready")

    accepted = {}
    pending_history = {}
    try:
        for thread in threads:
            accepted[thread] = [await send(case, session, thread, texts[thread][0])]
        # 不放行任一 Thread，两个工具结果仍须均已 durable，证明 sibling 可并行。
        for thread in threads:
            tool_call = calls[thread][0] + ":tool-call:read-common-source"
            await asyncio.to_thread(control.acknowledged, tool_call, "test.tool_result.committed", 30)
            result = case.tool_result(session, tool_call)
            assert result["tool_outcome"] == "success" and "shared source" in str(result["content"])
            accepted[thread].append(await send(case, session, thread, texts[thread][1]))
            head, queued = accepted[thread]
            assert queued.status == "queued" and queued.dispatch.active_job_id == head.job_id
            assert queued.dispatch.blocked_by_job_id == head.job_id
            assert queued.dispatch.queued_jobs_ahead == 0
            assert not (case.replay.artifacts / (calls[thread][2] + ".json")).exists()
            pending_history[thread] = await history(case, session, thread)
            users = {user.message_id: user for turn in pending_history[thread].items for user in turn.user_messages}
            assert set(users) == {initial.message_id, head.message_id, queued.message_id}
            assert [users[item.message_id].content for item in accepted[thread]] == list(texts[thread])

        # 相同正文来自四次独立产品上传，不能合并可变目录身份。
        users_by_thread = {thread: {user.message_id: user for turn in pending_history[thread].items
                                   for user in turn.user_messages} for thread in threads}
        refs = {thread: [users_by_thread[thread][item.message_id].attachments[0] for item in accepted[thread]]
                for thread in threads}
        directory_ids = {thread: {directory_id(ref.file_id) for ref in refs[thread]} for thread in threads}
        assert len(directory_ids[threads[0]] | directory_ids[threads[1]]) == 4
        before_read = associations(case)
        for thread in threads:
            for attachment_id in directory_ids[thread]:
                rows = [row for row in before_read if row[0] == attachment_id and row[4] == "active"]
                assert rows and {(row[1], row[2]) for row in rows} == {(session, thread)}
            for ref in refs[thread]:
                read = await case.client.get("/api/v1/workspace/files/content", params={"path": ref.file_id})
                assert read.status_code == 200 and read.json()["data"]["content"] == BODY.decode(), read.text
        assert associations(case) == before_read  # 工作区读取不额外建立 Thread 保活关联。

        child_before = state(roots[threads[1]])
        control.release(calls[threads[0]][0] + ":tool-call:read-common-source", "test.tool_result.committed")
        await _completed(case.client, accepted[threads[0]][0].job_id)
        await asyncio.to_thread(control.acknowledged, calls[threads[0]][2], "model.response_ready", 30)
        assert state(roots[threads[1]]) == child_before
        control.release(calls[threads[0]][2], "model.response_ready")
        await _completed(case.client, accepted[threads[0]][1].job_id)
        main_finished = state(roots[threads[0]])
        control.release(calls[threads[1]][0] + ":tool-call:read-common-source", "test.tool_result.committed")
        await _completed(case.client, accepted[threads[1]][0].job_id)
        await asyncio.to_thread(control.acknowledged, calls[threads[1]][2], "model.response_ready", 30)
        control.release(calls[threads[1]][2], "model.response_ready")
        await _completed(case.client, accepted[threads[1]][1].job_id)
        assert state(roots[threads[0]]) == main_finished

        for thread, sibling in zip(threads, reversed(threads), strict=True):
            requests = [json.dumps(case.request(call)["messages"], ensure_ascii=False) for call in calls[thread]]
            assert all(texts[thread][0] in request for request in requests)
            assert all(texts[thread][1] not in request for request in requests[:2])
            assert texts[thread][1] in requests[2]
            assert all(text not in request for text in texts[sibling] for request in requests)
            for ref in refs[thread][1:]:
                assert ref.file_id not in requests[1]
            assert all(ref.file_id not in request for ref in refs[sibling] for request in requests)

        finished = {thread: state(roots[thread]) for thread in threads}
        pages = {thread: await history(case, session, thread) for thread in threads}
        await case.hard_restart()
        for thread in threads:
            assert state(roots[thread]) == finished[thread]
            assert await history(case, session, thread) == pages[thread]
        assert len(case.planned_calls) == 7
        case.record("thread-attachment-isolation", {
            "session_id": session, "threads": threads,
            "acceptances": {thread: [item.model_dump(mode="json") for item in accepted[thread]] for thread in threads},
            "independent_directories": {thread: sorted(directory_ids[thread]) for thread in threads},
            "associations_before_read": before_read, "model_calls": calls,
            "scope": "真实 Gateway/Backend、消息/工具/fork、model_stream、独立 SQLite/JSONL 与硬重启；不代表 Web、Windows、GC 或全部 8.3 验收",
        })
    finally:
        for thread in threads:
            control.release(calls[thread][0] + ":tool-call:read-common-source", "test.tool_result.committed")
            control.release(calls[thread][2], "model.response_ready")
