"""真实历史的 canonical 原文失败保持错误归属、工具闭合及同会话恢复。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from contextlib import closing
from pathlib import Path

import pytest

from app.api.message_stream import _public_event_json
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_store import SessionControlStore
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.durable import (
    compaction_candidates,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实 Agent 与唯一模型协议装具。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.fixture
async def canonical_history(source_case: RuntimeSourceCase):
    case = source_case
    catalog = await case.client.get("/api/v1/tools", params={"agent_id": "default"})
    assert catalog.status_code == 200, catalog.text
    tool, = [item for item in catalog.json()["data"] if item["name"] == "read_compaction_history"]
    selected = await case.client.patch("/api/v1/tools/selection", json={
        "agent_id": "default", "changes": [
            {"tool_id": tool["tool_id"], "execution_enabled": True, "model_visible": True},
        ],
    })
    assert selected.status_code == 200, selected.text
    session = await create_session(case.client, "canonical 原文失败及同会话恢复")
    thread = await case.container.session_service.resolve_main_thread(session)
    replies = [f"canonical-history-answer-{index}:" + "已完成的详细记录。" * 60 for index in range(3)]
    accepted = []
    for index, reply in enumerate(replies):
        await case.plan(session, [{"content": reply}])
        turn = await _send(case, session, f"canonical-history-question-{index}")
        await _job(case, str(turn["job_id"]))
        accepted.append(turn)
    assert case.sql(session, thread, "SELECT COUNT(*) FROM context_compaction_candidates") == [(0,)]
    return case, session, thread, replies, accepted


async def _compaction_task(case: RuntimeSourceCase, session: str, thread: str, *, expected: str):
    response = await case.client.post(f"/api/v1/sessions/{session}/compact", params={"thread_id": thread})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    receipt = response.json()["data"]
    assert receipt["status"] == "scheduled" and receipt["summarized_message_count"] == 0
    async with asyncio.timeout(30):
        while True:
            handle = case.container.background_task_registry.get_handle(session, receipt["task_id"])
            assert handle is not None and handle.thread_id == thread
            if handle.status not in {"pending", "running"}:
                assert handle.status == expected, handle.metadata
                return handle
            await asyncio.sleep(.05)


async def _events(case: RuntimeSourceCase, session: str, thread: str, turn_id: str):
    response = await case.client.get(f"/api/v1/sessions/{session}/turns/{turn_id}/message-stream/events",
                                     params={"thread_id": thread})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


def _sealed_proof(case: RuntimeSourceCase, session: str, thread: str):
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    captures = {call: json.loads((case.model.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    return audit_sealed_requests(resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite",
                                 (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)


@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_canonical_history_read_failure_keeps_paired_result_and_same_thread_recovery(
    canonical_history, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    tmp_path: Path, log_fails: bool,
) -> None:
    case, session, thread, replies, _turns = canonical_history
    saver = case.container.checkpointer
    candidates = saver._storage.compactions
    # 未采用范围正常为空；先由真实后台生成和后继消息建立原文回读范围。
    assert candidates.read_history(session, thread) == ""
    await case.plan(session, [{"content": "已完成前两轮资料的冻结摘要。"}])
    handle = await _compaction_task(case, session, thread, expected="completed")
    assert candidates.read_history(session, thread) == ""
    await case.plan(session, [{"content": "已采用完整候选。"}])
    adopted = await _send(case, session, "采用已安排的候选后继续。")
    await _job(case, str(adopted["job_id"]))
    original_history = candidates.read_history(session, thread)
    assert replies[0] in original_history
    assert case.sql(session, thread, "SELECT applied_view_id FROM context_compaction_candidates WHERE task_id=?",
                    (handle.task_id,))[0][0] is not None
    root = saver._storage.storage.root(session, thread_id=thread)
    original_bytes = (root / "rollout.jsonl").read_bytes()
    blocker, log_blocker = tmp_path / "canonical-read-native", tmp_path / "canonical-log-secondary"
    blocker.write_text("原文页读取的原生故障", encoding="utf-8")
    log_blocker.write_text("日志输出的原生故障", encoding="utf-8")
    causes: list[OSError] = []
    sink_errors: list[OSError] = []
    logged: list[str] = []
    capture = caplog.handler.emit

    def fail_page(*_args, **_kwargs):
        try:
            return (blocker / "original.jsonl").open("rb")
        except OSError as error:
            causes.append(error)
            raise

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if record.name != "app.core.failures" or "stage=tool-execution" not in record.getMessage():
            return
        logged.append(record.getMessage())
        if log_fails:
            try:
                (log_blocker / "service.log").write_text(record.getMessage(), encoding="utf-8")
            except OSError as error:
                sink_errors.append(error)
                raise

    wire_id = "read-canonical-failed"
    call, = await case.plan(session, [_tool_reply("read_compaction_history", {"line_offset": 1, "limit": 200}, wire_id)])
    with monkeypatch.context() as patch:
        patch.setattr(compaction_candidates, "read_history_page", fail_page)
        patch.setattr(caplog.handler, "emit", emit)
        failed = await _send(case, session, "读取已采用摘要对应的原文。")
        job = await _job(case, str(failed["job_id"]), expected="failed")
    operation = call + ":tool-call:" + wire_id
    item, = [item for item in saver._storage.storage.read_items(session, thread)
             if item.semantic_kind == "tool_result" and isinstance(item.payload, dict)
             and item.payload["tool_call_id"] == operation]
    # handler 异常没有成功回执；沿工具执行合同保留 unknown，并单独核失败归属。
    assert item.payload["tool_outcome"] == "unknown"
    failure = item.payload["content"]
    assert "stage=tool-execution" in failure and "NotADirectoryError" in failure
    assert "read_compaction_history" in failure and str(blocker / "original.jsonl") in failure
    assert "NotADirectoryError" in job["error_message"]
    cause, = causes
    assert cause.errno == errno.ENOTDIR
    logged_message, = logged
    assert all(fragment in logged_message for fragment in (
        "stage=tool-execution", "read_compaction_history", "NotADirectoryError",
        str(blocker / "original.jsonl"), f"call_id={wire_id}",
    ))
    if log_fails:
        sink_error, = sink_errors
        assert sink_error.errno == errno.ENOTDIR and "diagnostic_sink_error=NotADirectoryError" in failure
        assert any(str(sink_error) in note for note in cause.__notes__)
    else:
        assert not sink_errors and "diagnostic_sink_error" not in failure
    assert str(log_blocker) not in failure
    failed_events = await _events(case, session, thread, str(failed["turn_id"]))
    stream_id = failed_events[-1]["turn_stream_id"]
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    cold = MessageStreamStore(path_resolver=resolver)
    await cold.open_existing(session_id=session, thread_id=thread, turn_id=str(failed["turn_id"]), turn_stream_id=stream_id)
    assert [_public_event_json(event) for event in await cold.list_events(
        session_id=session, turn_stream_id=stream_id,
    )] == failed_events
    completed, = [event for event in failed_events if event["type"] == "tool.completed"]
    assert completed["payload"]["status"] == "failed"
    assert completed["payload"]["outcome"] == "outcome_unknown"
    assert completed["payload"]["completion_reason"] == "execution_error"
    assert (await cold.get_state(stream_id))["stream_status"] == "failed"

    _, followup = await case.plan(session, [
        _tool_reply("read_compaction_history", {"line_offset": 1, "limit": 200}, "read-canonical-recovered"),
        {"content": "原文读取恢复，继续同一会话。"},
    ])
    resumed = await _send(case, session, "读取原文后继续。")
    assert resumed["thread_id"] == thread
    await _job(case, str(resumed["job_id"]))
    messages = case.request(followup)["messages"]
    paired, = [message for message in messages if message["role"] == "tool" and message["tool_call_id"] == wire_id]
    recovered, = [message for message in messages if message["role"] == "tool" and message["tool_call_id"] == "read-canonical-recovered"]
    assert paired["content"] == failure and recovered["content"] == original_history
    assert (root / "rollout.jsonl").read_bytes().startswith(original_bytes)
    assert [_public_event_json(event) for event in await cold.list_events(
        session_id=session, turn_stream_id=stream_id,
    )] == failed_events
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold_saver:
        stored, = cold_saver._storage.storage.read_items(session, thread, item_ids=(item.item_id,))
        assert stored.to_dict() == item.to_dict()
        assert cold_saver._storage.compactions.read_history(session, thread) == original_history
    case.write_actual(f"canonical-history-read-failure-{log_fails}", {
        "failed_job": job, "tool_result": item.to_dict(), "failed_events": failed_events,
        "recovered": resumed, "diagnostic_sink_errors": len(sink_errors),
        "sealed_requests": _sealed_proof(case, session, thread),
    })


async def test_background_canonical_source_failure_does_not_fail_turn_and_manual_request_recovers(
    canonical_history, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    case, session, thread, replies, turns = canonical_history
    saver = case.container.checkpointer
    storage, candidates = saver._storage.storage, saver._storage.compactions
    original_events = await _events(case, session, thread, str(turns[-1]["turn_id"]))
    original_bytes = storage.jsonl_path(session, thread_id=thread).read_bytes()
    original_calls = case.call_count(session, thread)
    missing = tmp_path / "canonical-original-missing.jsonl"
    log_blocker = tmp_path / "background-log-secondary"
    log_blocker.write_text("日志输出的原生故障", encoding="utf-8")
    causes: list[OSError] = []
    sink_errors: list[OSError] = []
    consumed_refs = []
    capture = caplog.handler.emit

    def fail_source(*_args, **kwargs):
        consumed_refs.extend(kwargs["refs"])
        try:
            return missing.open("rb")
        except OSError as error:
            causes.append(error)
            raise

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if record.name != "app.core.failures" or "stage=background-task-execution" not in record.getMessage():
            return
        try:
            (log_blocker / "service.log").write_text(record.getMessage(), encoding="utf-8")
        except OSError as error:
            sink_errors.append(error)
            raise

    with monkeypatch.context() as patch:
        patch.setattr(storage, "read_ref_items", fail_source)
        patch.setattr(caplog.handler, "emit", emit)
        failed = await _compaction_task(case, session, thread, expected="failed")
    cause, = causes
    sink_error, = sink_errors
    assert cause.errno == errno.ENOENT and cause.filename == str(missing)
    assert sink_error.errno == errno.ENOTDIR and any(str(sink_error) in note for note in cause.__notes__)
    assert consumed_refs and all((ref.session_id, ref.thread_id) == (session, thread) for ref in consumed_refs)
    note = failed.metadata["status_note"]
    assert "stage=background-task-execution" in note and "FileNotFoundError" in note
    assert str(missing) in note and "diagnostic_sink_error=NotADirectoryError" in note
    assert case.call_count(session, thread) == original_calls
    assert case.sql(session, thread, "SELECT discarded_reason,applied_view_id FROM context_compaction_candidates WHERE task_id=?",
                    (failed.task_id,)) == [("task-failed", None)]
    assert candidates.read_history(session, thread) == ""
    assert storage.jsonl_path(session, thread_id=thread).read_bytes() == original_bytes
    assert await _events(case, session, thread, str(turns[-1]["turn_id"])) == original_events

    await case.plan(session, [{"content": "后台原文不可读未改变正常对话。"}])
    continued = await _send(case, session, "先继续当前对话，不重做后台摘要。")
    await _job(case, str(continued["job_id"]))
    continued_events = await _events(case, session, thread, str(continued["turn_id"]))
    assert not [event for event in continued_events if event["type"] in {"activity.failed", "stream.failed"}]
    assert case.sql(session, thread, "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='compaction_summary'") == [(0,)]
    await case.plan(session, [{"content": "已重新完整概括冻结历史。"}])
    recovered = await _compaction_task(case, session, thread, expected="completed")
    assert recovered.task_id != failed.task_id
    await case.plan(session, [{"content": "新候选已采用，同会话恢复。"}])
    adopted = await _send(case, session, "采用刚安排的完整候选后继续。")
    await _job(case, str(adopted["job_id"]))
    assert replies[0] in candidates.read_history(session, thread)
    assert case.sql(session, thread, "SELECT COUNT(*) FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL") == [(1,)]
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    with closing(SessionControlStore(resolver.resolve_session_node(session) / "session-control.sqlite", read_only=True)) as control:
        cold_task = control.get_background_task(failed.task_id)
        assert cold_task.status == "failed" and cold_task.metadata["status_note"] == note
    cold = MessageStreamStore(path_resolver=resolver)
    stream_id = original_events[-1]["turn_stream_id"]
    await cold.open_existing(session_id=session, thread_id=thread, turn_id=str(turns[-1]["turn_id"]), turn_stream_id=stream_id)
    assert [_public_event_json(event) for event in await cold.list_events(
        session_id=session, turn_stream_id=stream_id,
    )] == original_events
    assert (await cold.get_state(stream_id))["stream_status"] == "completed"
    case.write_actual("background-canonical-source-failure", {
        "failed_task": failed.to_dict(), "continued": continued, "adopted": adopted,
        "original_events": original_events, "diagnostic_sink_errors": len(sink_errors),
        "sealed_requests": _sealed_proof(case, session, thread),
    })
