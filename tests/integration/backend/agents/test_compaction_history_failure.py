"""归档不可读时，日志次错不改变真实 Agent 的工具结果和后续执行。"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实 Agent 与唯一模型协议装具
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_missing_archive_keeps_tool_failure_and_continues_after_log_error(
    source_case: RuntimeSourceCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    log_fails: bool,
) -> None:
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
    session = await create_session(case.client, "归档读取失败及诊断日志次错")
    main = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    session_root = resolver.resolve_session_node(session)
    archive = session_root / "context" / thread / "history.md"
    assert not archive.exists()
    blocker = tmp_path / "archive-log-secret"
    blocker.write_text("普通文件不能承载日志子文件", encoding="utf-8")
    destination = blocker / "service.log"
    capture = caplog.handler.emit
    failures: list[OSError] = []
    diagnostic_causes: list[BaseException] = []

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if not (record.name == "app.agents.summarization.tool_middleware" or
                record.name == "app.core.failures" and "compaction-history-read" in record.getMessage()):
            return
        if record.exc_info and isinstance(record.exc_info[1], BaseException):
            diagnostic_causes.append(record.exc_info[1])
        if log_fails:
            try:
                destination.write_text(record.getMessage(), encoding="utf-8")
            except OSError as error:
                failures.append(error)
                raise

    results = []
    for index in range(2):
        wire_id = f"read-missing-{index}"
        call, followup = await case.plan(session, [
            _tool_reply("read_compaction_history", {"line_offset": 1, "limit": 200}, wire_id),
            {"content": f"archive-unavailable-ack-{index}"},
        ])
        with monkeypatch.context() as patch:
            if index == 0:
                patch.setattr(caplog.handler, "emit", emit)
            accepted = await _send(case, session, f"读取当前线程归档，第{index}次")
            assert accepted["thread_id"] == thread
            job = await _job(case, str(accepted["job_id"]))
        operation = call + ":tool-call:" + wire_id
        event, = [item for item in case.control.events
                  if item["phase"] == "test.tool_result.committed" and item["operation_id"] == operation]
        details = event["details"]
        assert isinstance(details, dict)
        item = details["item"]
        assert isinstance(item, dict)
        persisted, = case.container.checkpointer._storage.storage.read_items(
            session, thread, item_ids=(item["item_id"],),
        )
        assert persisted.semantic_kind == "tool_result"
        assert persisted.payload == item["payload"]
        assert isinstance(persisted.payload, dict)
        assert persisted.payload["tool_call_id"] == operation
        result = persisted.payload["content"]
        assert isinstance(result, str)
        assert "detail-unavailable" in result and "line_offset=1" in result
        messages = case.request(followup)["messages"]
        assert isinstance(messages, list)
        tool_message, = [message for message in messages if message["role"] == "tool" and message["tool_call_id"] == wire_id]
        assert tool_message["content"] == result
        tool_call, = [tool for message in messages for tool in message.get("tool_calls", []) if tool["id"] == wire_id]
        assert tool_call["function"]["name"] == "read_compaction_history"
        assert str(session_root) not in result and "history.md" not in result and "archive-log-secret" not in result
        assert not archive.exists()
        results.append({"job": job, "tool_item": persisted.to_dict(), "followup_call": followup})
    assert len(diagnostic_causes) == 1
    cause = diagnostic_causes[0]
    assert isinstance(cause, RuntimeError) and "history.md" in str(cause)
    if log_fails:
        assert len(failures) == 1 and failures[0].errno == errno.ENOTDIR
        assert any(str(failures[0]) in note for note in cause.__notes__)
    else:
        assert not failures and not getattr(cause, "__notes__", [])
    assert results[0]["tool_item"]["payload"]["content"] == results[1]["tool_item"]["payload"]["content"]
    root = resolver.resolve_thread_node(session, thread) / "rollout"
    assert str(session_root).encode() not in (root / "rollout.jsonl").read_bytes()
    assert b"archive-log-secret" not in (root / "rollout.jsonl").read_bytes()
    captures = {call: json.loads((case.model.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    proof = audit_sealed_requests(root / "index.sqlite", (session_root / "logs/llm_requests").glob("*.json"), captures)
    case.write_actual(f"compaction-history-failure-{log_fails}", {
        "results": results, "sealed_requests": proof, "diagnostic_sink_errors": len(failures),
    })
