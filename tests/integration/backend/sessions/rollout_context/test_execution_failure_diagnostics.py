"""诊断写入失败仍经产品入口收敛原执行，后继输入沿同一 Thread 执行。"""

from __future__ import annotations

import errno
import json
from collections.abc import Callable, Mapping

import pytest
from langchain_core.messages import ToolMessage

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.serialization import normalize_wire_request
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 使用当前测试的隔离工作区。
)
from tests.integration.backend.sessions.test_tool_failure_facts import (
    fail_after_read as fail_after_read,  # noqa: PLC0414 - 在真实读取成功后注入原异常。
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 真实日志写入产生 ENOTDIR。
)


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_failed_diagnostic_preserves_execution_terminal_and_same_thread_successor(
    source_case: RuntimeSourceCase,
    diagnostic_sink: DiagnosticFailure,
    fail_after_read: Callable[[BaseException], list[ToolMessage]],
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    (case.workspace / "failure-input.txt").write_text("真实文件工具仍执行一次读取。", encoding="utf-8")
    original = OSError(errno.EIO, "原执行读取失败", "/private/execution-secret")
    diagnostic_sink.primary = original
    observations = fail_after_read(original)
    first_call, = await case.plan(session_id, [
        _tool_reply("read_file", {"path": "failure-input.txt"}, "failed-read"),
    ])
    accepted = await _send(case, session_id, "读取材料并保留原失败的结算事实。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    failed_job = await _job(case, job_id, expected="failed")
    assert len(observations) == 1 and case.call_count(session_id, thread_id) == 1
    assert failed_job["error_message"] == (
        "执行系统 I/O 失败: error=OSError, errno=5, reason=原执行读取失败, "
        "filename='/private/execution-secret', diagnostic_sink_error=NotADirectoryError"
    )
    assert diagnostic_sink.errors and all(error.errno == errno.ENOTDIR for error in diagnostic_sink.errors)
    assert all(record.exc_info and record.exc_info[1] is original for record in diagnostic_sink.records)
    assert any(str(diagnostic_sink.errors[0]) in note for note in original.__notes__)
    storage = case.container.checkpointer._storage.storage
    items = storage.read_items(session_id, thread_id)
    result, = [item for item in items if item.semantic_kind == "tool_result"]
    assert isinstance(result.payload, Mapping)
    assert result.payload["tool_call_id"] == first_call + ":tool-call:failed-read"
    assert result.payload["name"] == "read_file"
    assert result.payload["tool_outcome"] == "unknown" and result.payload["execution_confirmed"] is False
    terminal_query = (
        "SELECT e.execution_id,e.outcome,t.status,q.phase FROM executions e "
        "JOIN turn_records t ON t.turn_id=e.turn_id "
        "JOIN execution_queue q ON q.execution_id=e.execution_id WHERE e.job_id=?"
    )
    terminal = case.sql(session_id, thread_id, terminal_query, (job_id,))
    assert len(terminal) == 1 and terminal[0][1:] == ("failed", "failed", "terminal")
    stream_url = f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream/snapshot"
    snapshot = await case.client.get(stream_url, params={"thread_id": thread_id})
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.headers["X-Request-ID"] == snapshot.json()["request_id"]
    frozen_stream = snapshot.json()["data"]
    assert frozen_stream["stream_status"] == "failed"
    frozen_items = canonical_json_bytes([item.to_dict() for item in items])
    catalog_query = "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence"
    original_catalog = case.sql(session_id, thread_id, catalog_query)

    # 解除诊断故障后只发送新输入，不补写旧执行的任何状态。
    diagnostic_sink.primary = None
    second_call, = await case.plan(session_id, [{"content": "后继执行已完成。"}])
    successor = await _send(case, session_id, "继续同一 Thread，读取已保留的失败事实。")
    successor_id = successor["job_id"]
    assert isinstance(successor_id, str) and successor_id != job_id
    await _job(case, successor_id)
    assert len(observations) == 1 and case.call_count(session_id, thread_id) == 2
    assert case.sql(session_id, thread_id, terminal_query, (job_id,)) == terminal
    assert case.sql(session_id, thread_id, catalog_query)[:len(original_catalog)] == original_catalog
    restored_items = storage.read_items(session_id, thread_id, item_ids=tuple(item.item_id for item in items))
    assert canonical_json_bytes([item.to_dict() for item in restored_items]) == frozen_items
    restored_stream = await case.client.get(stream_url, params={"thread_id": thread_id})
    assert restored_stream.status_code == 200 and restored_stream.json()["data"] == frozen_stream
    request = case.request(second_call)
    messages = request["messages"]
    assert isinstance(messages, list)
    tool_result, = [message for message in messages if message["role"] == "tool"]
    assert tool_result["tool_call_id"] == "failed-read" and tool_result["content"] == result.payload["content"]
    previous = messages[messages.index(tool_result) - 1]
    assert [call["id"] for call in previous["tool_calls"]] == ["failed-read"]
    assemblies = case.container.checkpointer._storage.assemblies.list_context_assemblies(session_id, thread_id)
    assert {assembly.model_call_id for assembly in assemblies} == {first_call, second_call}
    for assembly in assemblies:
        assert (assembly.session_id, assembly.thread_id) == (session_id, thread_id)
        assert isinstance(assembly.model_call_id, str)
        assert assembly.request_hash_preimage == normalize_wire_request(case.request(assembly.model_call_id))
        assembly.validate_hashes()
    public = json.dumps([failed_job, frozen_stream, result.payload, case.requests()], ensure_ascii=False)
    assert "execution-secret" in public and "diagnostic-log-secret" not in public
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        persisted = "\n".join(connection.iterdump())
    assert "execution-secret" not in persisted and "diagnostic-log-secret" not in persisted
    case.write_actual("execution-failure-diagnostic", {
        "failed_job": failed_job, "terminal": terminal, "stream": frozen_stream,
        "tool_result": result.payload, "calls": [first_call, second_call],
        "diagnostic_failures": len(diagnostic_sink.errors),
    })
