"""通过真实消息、回放与写锁屏障验证前缀捕获的一致性。"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    _first_response_item,
    _send_thread,
    _wire_text,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    deadlines as deadlines,  # noqa: PLC0414 - 复用固定60秒fork监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实服务及model_stream
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry


@pytest.mark.parametrize("source_case", [{
    "managed_gateway": True, "terminal": True, "browser": True,
    "workspace_template": "model_tool_test_workspace",
}], indirect=True)
@pytest.mark.parametrize("change,anchor_mode", [("append", "inclusive"), ("replay", "before")])
async def test_prefix_capture_serializes_source_changes_and_reuses_original_point(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
    monkeypatch: pytest.MonkeyPatch, change: str, anchor_mode: str,
) -> None:
    case = source_case
    session = await create_session(case.client, "并发前缀冻结")
    await case.plan(session, [{"content": text} for text in (
        "consistent-first-answer", "consistent-second-answer",
        "consistent-before-capture-answer", "consistent-after-capture-answer",
    )])
    first = await _send(case, session, "consistent-first-question")
    thread = (await _job(case, first["job_id"]))["thread_id"]
    second = await _send(case, session, "consistent-second-question")
    await _job(case, second["job_id"])
    item_id = _first_response_item(case, session, thread)
    endpoint = f"/api/v1/sessions/{session}/fork-source-point"
    params = {"thread_id": thread, "item_id": item_id}
    observed_response = await case.client.get(endpoint, params=params)
    assert observed_response.status_code == 200, observed_response.text
    observed = observed_response.json()["data"]
    key = f"consistent-prefix-{change}-{anchor_mode}"
    creation_key = case.container.session_context_fork_service._creation_key(key)
    body = {
        "idempotency_key": key, "mode": "view_prefix_fork",
        "source_uri": observed["source_uri"], "target_container_uri": observed["child_container_uri"],
        "source_view_revision": observed["source_view_revision"],
        "anchor_item_id": item_id, "anchor_mode": anchor_mode,
    }
    phases = ("thread.creation.prepared", "copy.source.partial")
    for phase in phases:
        case.control.arm(creation_key, phase)
    deadline = deadlines.for_operation(target=observed["child_container_uri"], operation_key=key)
    fork = asyncio.create_task(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=body))
    writer = None
    try:
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, creation_key, phases[0]), "原创建记录准备")
        factory = case.container.thread_initial_execution_service._factory
        catalog = case.container.session_service.path_resolver.catalog_store
        with factory.for_owner_session(session) as owner:
            prepared = owner.control_store.get_thread_creation_record(creation_key)
            assert json.loads(prepared.request_intent_json)["source_point"] is None
            assert not owner.control_store.list_child_thread_rows()
        assert catalog.get_fork_source_capture(creation_key=creation_key) is None

        # 创建 intent 后、捕获前，通过产品入口推进 revision 或切换来源分支。
        if change == "append":
            changed = await deadline.wait(_send(case, session, "consistent-before-capture-question"), "捕获前追加")
        else:
            replay = await deadline.wait(case.client.post(
                f"/api/v1/sessions/{session}/messages/{second['message_id']}/replay", json={
                    "action": "edit_and_continue", "content": "consistent-before-capture-question",
                    "acknowledge_context_only": True,
                },
            ), "捕获前切换分支")
            assert replay.status_code == 200, replay.text
            changed = replay.json()["data"]
        await deadline.wait(_job(case, changed["job_id"]), "捕获前执行完成")
        candidate_response = await deadline.wait(case.client.get(endpoint, params=params), "读取捕获前候选")
        assert candidate_response.status_code == 200, candidate_response.text
        candidate = candidate_response.json()["data"]
        assert candidate["source_view_id"] != observed["source_view_id"]
        assert candidate["source_view_revision"] > observed["source_view_revision"]
        case.control.release(creation_key, phases[0])
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, creation_key, phases[1]), "私有快照已复制")

        source_root = case.container.checkpointer._storage.storage.root(session, thread_id=thread)
        lock_path = (source_root.parent / ".rollout.write.lock").resolve()
        writer_attempted, writer_acquired = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_acquire_sync = _RolloutFileLock.acquire
        original_acquire = _RolloutFileLock.acquire_async

        def observe_sync_writer(lock: _RolloutFileLock) -> None:
            source_writer = lock._path == lock_path and lock._exclusive
            if source_writer:
                loop.call_soon_threadsafe(writer_attempted.set)
            original_acquire_sync(lock)
            if source_writer:
                writer_acquired.set()

        async def observe_writer(lock: _RolloutFileLock) -> None:
            source_writer = lock._path == lock_path and lock._exclusive
            if source_writer:
                writer_attempted.set()
            await original_acquire(lock)
            if source_writer:
                writer_acquired.set()

        # 只观察真实锁，不替换准入、写入或返回值。
        monkeypatch.setattr(_RolloutFileLock, "acquire", observe_sync_writer)
        monkeypatch.setattr(_RolloutFileLock, "acquire_async", observe_writer)
        writer = asyncio.create_task(_send(case, session, "consistent-after-capture-question"))
        await deadline.wait(writer_attempted.wait(), "来源 writer 已尝试独占锁")
        assert not writer_acquired.is_set()
        assert not writer.done()
        assert case.sql(session, thread, "SELECT history_view_revision FROM database_meta WHERE singleton_id=1") == [
            (candidate["source_view_revision"],),
        ]
        case.control.release(creation_key, phases[1])
        response = await deadline.wait(fork, "发布原前缀")
        assert response.status_code == 200, response.text
        deadline.complete()
        after = await writer
        await _job(case, after["job_id"])
    finally:
        for phase in phases:
            case.control.release(creation_key, phase)
        for task in (fork, writer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (fork, writer) if task is not None), return_exceptions=True)

    result = response.json()["data"]
    capture = catalog.get_fork_source_capture(creation_key=creation_key)
    assert capture.captured_point.source_view_id == result["source_view_id"] == candidate["source_view_id"]
    assert capture.captured_point.source_view_revision == result["source_view_revision"] == candidate["source_view_revision"]
    assert capture.captured_point.anchor_item_id == result["anchor_item_id"] == item_id
    assert json.loads(capture.request_json)["source_view_revision"] == observed["source_view_revision"]
    comparison = "<=" if anchor_mode == "inclusive" else "<"
    expected = case.sql(session, thread,
        "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 "
        f"AND context_ordinal{comparison}(SELECT context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) "
        "ORDER BY context_ordinal", (candidate["source_view_id"], candidate["source_view_id"], item_id))
    actual = case.sql(session, result["thread_id"],
        "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
        "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
        "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal")
    assert actual == expected
    latest = await case.client.get(endpoint, params=params)
    assert latest.status_code == 200, latest.text
    assert latest.json()["data"]["source_view_id"] != candidate["source_view_id"]
    retry_deadline = deadlines.for_operation(target=observed["child_container_uri"], operation_key=key + "-retry")
    retry = await retry_deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=body), "重试原冻结结果")
    assert retry.status_code == 200, retry.text
    retry_deadline.complete()
    assert retry.json()["data"] == result
    assert catalog.get_fork_source_capture(creation_key=creation_key) == capture
    with factory.for_owner_session(session) as owner:
        record = owner.control_store.get_thread_creation_record(creation_key)
        assert record.child_thread_id == prepared.child_thread_id == result["thread_id"]
        assert record.request_intent_json == prepared.request_intent_json
        assert len(owner.control_store.list_child_thread_rows()) == 1
        assert not owner.control_store.list_initial_execution_intents()

    child_call, = await case.plan(session, [{"content": "consistent-child-answer"}], thread_id=result["thread_id"])
    child = await _send_thread(case, session, result["thread_id"], "consistent-child-question")
    await _job(case, child["job_id"])
    wire = _wire_text(case.request(child_call)["messages"])
    assert "consistent-first-question" in wire
    assert ("consistent-first-answer" in wire) == (anchor_mode == "inclusive")
    for excluded in ("consistent-second", "consistent-before-capture", "consistent-after-capture"):
        assert excluded not in wire
    case.write_actual(f"prefix-consistency-{change}-{anchor_mode}", {
        "observed": observed, "candidate": candidate, "result": result,
        "later": latest.json()["data"], "capture_id": capture.snapshot_id,
        "marker_hash": capture.marker_sha256, "expected": expected, "actual": actual,
        "writer_waited_for_capture": writer_acquired.is_set(), "child_call": child_call,
    })
