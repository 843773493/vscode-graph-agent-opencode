"""真实消息入口的工具定义写入拒绝、诊断次错及同会话恢复。"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import parse_session_artifact_uri
from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用原 Agent 与 model_stream 装具。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_definition_write_and_log_failure_keep_pairing_and_same_session_recovery(
    source_case: RuntimeSourceCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    case = source_case
    session = await create_session(case.client, "完整工具定义写入失败后恢复")
    main = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    session_root = resolver.resolve_session_node(session)
    blocker = session_root / "tool-results"
    assert not blocker.exists()
    blocker.write_text("保留定义目录冲突原件", encoding="utf-8")
    sink_blocker = tmp_path / "definition-log-secret"
    sink_blocker.write_text("日志目录也是普通文件", encoding="utf-8")
    destination = sink_blocker / "service.log"
    capture = caplog.handler.emit
    causes: list[BaseException] = []
    sink_errors: list[OSError] = []

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if record.name != "app.services.infrastructure.mcp.tool_search":
            return
        assert record.exc_info is not None and record.exc_info[1] is not None
        causes.append(record.exc_info[1])
        try:
            destination.write_text(record.getMessage(), encoding="utf-8")
        except OSError as error:
            sink_errors.append(error)
            raise

    search = {"tool_ids": ["read_context"], "max_response_bytes": 2048}
    first, followup = await case.plan(session, [
        _tool_reply("search_tools", search, "definition-write-failed"),
        {"content": "已收到定义保存失败，不使用虚假引用。"},
    ])
    with monkeypatch.context() as patch:
        patch.setattr(caplog.handler, "emit", emit)
        accepted = await _send(case, session, "检索上下文读取能力的完整参数定义，返回定义或保存失败即可。")
        await _job(case, str(accepted["job_id"]))
    cause, = causes
    sink_error, = sink_errors
    assert type(cause) is FileExistsError and cause.errno == errno.EEXIST
    assert isinstance(cause.__cause__, FileExistsError)
    assert cause.__cause__.filename == str(blocker)
    assert sink_error.errno == errno.ENOTDIR
    assert any(str(sink_error) in note for note in cause.__notes__)
    operation = first + ":tool-call:definition-write-failed"
    event, = [item for item in case.control.events
              if item["phase"] == "test.tool_result.committed" and item["operation_id"] == operation]
    details = event["details"]
    assert isinstance(details, dict)
    failed_item = details["item"]
    assert isinstance(failed_item, dict)
    stored, = case.container.checkpointer._storage.storage.read_items(
        session, thread, item_ids=(failed_item["item_id"],),
    )
    assert stored.semantic_kind == "tool_result" and stored.payload == failed_item["payload"]
    assert isinstance(stored.payload, dict)
    assert stored.payload["tool_call_id"] == operation
    assert stored.payload["tool_outcome"] == "failure"
    failure = "definition-materialization-failed: error_type=FileExistsError, diagnostic_sink_error=NotADirectoryError"
    assert stored.payload["content"] == failure
    failed_messages = case.request(followup)["messages"]
    assert isinstance(failed_messages, list)
    failed_message, = [message for message in failed_messages
                       if message["role"] == "tool" and message["tool_call_id"] == "definition-write-failed"]
    assert failed_message["content"] == failure
    assert blocker.read_text(encoding="utf-8") == "保留定义目录冲突原件"
    frozen_failure = stored.to_dict()

    # 修复原生目录后由同一消息入口重试；不预填定义正文或恢复状态。
    blocker.unlink()
    _, recovered_call = await case.plan(session, [
        _tool_reply("search_tools", search, "definition-write-recovered"),
        {"content": "已取得完整定义的真实资源引用。"},
    ])
    accepted = await _send(case, session, "目录已恢复，再次检索相同工具。")
    assert accepted["thread_id"] == thread
    await _job(case, str(accepted["job_id"]))
    recovered_messages = case.request(recovered_call)["messages"]
    assert isinstance(recovered_messages, list)
    recovered_message, = [message for message in recovered_messages
                          if message["role"] == "tool" and message["tool_call_id"] == "definition-write-recovered"]
    definition, = json.loads(recovered_message["content"])["results"]
    assert definition["tool_id"] == "read_context" and definition["schema_state"] == "needs_read"
    assert "input_schema" not in definition
    assert definition["schema_ref"] == definition["read_path"]
    parsed = parse_session_artifact_uri(definition["read_path"], local_gateway_id=case.gateway_id)
    assert parsed.tail_segments[:2] == (session, "tool-results")
    artifact = blocker / parsed.tail_segments[2]
    encoded = artifact.read_bytes()
    assert len(encoded) == definition["byte_count"] and len(encoded) > 2048
    assert hashlib.sha256(encoded).hexdigest() == definition["content_sha256"]
    document = json.loads(encoded)
    assert document["provenance"] == definition["provenance"]
    assert extension_schema_hash(document["input_schema"]) == definition["schema_hash"] == document["schema_hash"]
    assert document["input_schema"]["required"] == ["resource_identity", "uri", "scope"]
    assert document["input_schema"]["additionalProperties"] is False
    _, read_call = await case.plan(session, [
        _tool_reply("read_file", {"path": definition["read_path"]}, "definition-read"),
        {"content": "已读取完整工具定义。"},
    ])
    accepted = await _send(case, session, "用普通文件入口读取刚返回的定义引用。")
    await _job(case, str(accepted["job_id"]))
    messages = case.request(read_call)["messages"]
    assert isinstance(messages, list)
    read_message, = [message for message in messages
                    if message["role"] == "tool" and message["tool_call_id"] == "definition-read"]
    text = read_message["content"]
    assert not text.startswith("Error:") and "内容已截断" not in text
    actual = "".join(line.split("\t", 1)[1] for line in text.splitlines() if "\t" in line)
    assert actual.encode() == encoded
    original_message, = [message for message in messages
                         if message["role"] == "tool" and message["tool_call_id"] == "definition-write-failed"]
    assert original_message["content"] == failure
    unchanged, = case.container.checkpointer._storage.storage.read_items(
        session, thread, item_ids=(stored.item_id,),
    )
    assert unchanged.to_dict() == frozen_failure
    rollout = resolver.resolve_thread_node(session, thread) / "rollout"
    for body in (json.dumps(messages, ensure_ascii=False).encode(), (rollout / "rollout.jsonl").read_bytes(), encoded):
        assert str(session_root).encode() not in body and b"definition-log-secret" not in body
    captures = {call: json.loads((case.model.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    proof = audit_sealed_requests(rollout / "index.sqlite", (session_root / "logs/llm_requests").glob("*.json"), captures)
    assert case.call_count(session, thread) == 6
    case.write_actual("tool-search-storage-failure", {
        "failure_item": frozen_failure, "definition": definition,
        "sealed_requests": proof, "diagnostic_sink_errors": len(sink_errors),
    })
