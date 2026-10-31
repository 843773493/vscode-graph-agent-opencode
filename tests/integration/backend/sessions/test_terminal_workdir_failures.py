"""原生目录解析失败经真实执行入口落盘，修复后同一 Thread 可继续运行。"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping

import pytest

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
    source_case as source_case,  # noqa: PLC0414 - 使用当前文件的隔离工作区和真实 Terminal。
)


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 符号链接循环；Windows 原生路径另行验收")
@pytest.mark.parametrize("source_case", [{"terminal": True, "managed_gateway": True}], indirect=True)
async def test_native_workdir_failure_is_private_durable_and_recoverable(
    source_case: RuntimeSourceCase, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    directory = case.workspace / "native-cwd-loop"
    directory.symlink_to(directory.name)
    first_call, = await case.plan(session_id, [
        _tool_reply("exec_command", {"cmd": "cat probe.txt", "workdir": directory.name}, "cwd-failure"),
    ])
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "在指定目录执行命令，保留真实失败。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        failed_job = await _job(case, job_id, expected="failed")
    assert str(case.workspace) not in str(failed_job["error_message"])
    assert "stage=terminal_workdir" in str(failed_job["error_message"])
    owner = case.container.terminal_resource_owner
    assert not owner._store.ids() and not await owner._transport.list_resources()
    storage = case.container.checkpointer._storage.storage
    items = storage.read_items(session_id, thread_id)
    result, = [item for item in items if item.semantic_kind == "tool_result"]
    assert isinstance(result.payload, Mapping)
    assert result.payload["tool_call_id"] == first_call + ":tool-call:cwd-failure"
    assert result.payload["name"] == "exec_command"
    frozen_items = canonical_json_bytes([item.to_dict() for item in items])
    stream_path = f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream/snapshot"
    response = await case.client.get(stream_path, params={"thread_id": thread_id})
    assert response.status_code == 200 and response.headers["X-Request-ID"] == response.json()["request_id"]
    frozen_stream = response.json()["data"]
    assert frozen_stream["stream_status"] == "failed"
    public = json.dumps([failed_job, frozen_stream, result.payload, case.requests()], ensure_ascii=False)
    assert str(directory) not in public and str(case.workspace) not in public
    checkpoints = list(case.container.checkpointer.list(None))
    assert str(case.workspace) not in repr(checkpoints)
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        assert str(case.workspace) not in "\n".join(connection.iterdump())
    assert any(record.name == "app.core.failures" and record.exc_info
               and str(directory) in str(record.exc_info[1]) for record in caplog.records)

    # 只修复用户指定的原生目录，再由同一消息入口创建并执行真实 PTY。
    directory.unlink()
    directory.mkdir()
    (directory / "probe.txt").write_text("NATIVE_CWD_RECOVERED", encoding="utf-8")
    second, final = await case.plan(session_id, [
        _tool_reply("exec_command", {"cmd": "cat probe.txt", "workdir": directory.name,
                                    "yield_time_ms": 1000}, "cwd-recovered"),
        {"content": "指定目录恢复后已执行。"},
    ])
    successor = await _send(case, session_id, "原目录已修复，重新执行同一命令。")
    successor_id = successor["job_id"]
    assert isinstance(successor_id, str) and successor_id != job_id
    await _job(case, successor_id)
    executed = case.tool_result(second + ":tool-call:cwd-recovered")
    output, terminal_id = executed["output"], executed["terminal_id"]
    assert isinstance(output, str) and isinstance(terminal_id, str)
    assert executed["exit_code"] == 0 and "NATIVE_CWD_RECOVERED" in output
    assert executed["cwd_relative"] == directory.name
    terminal = await owner.get_resource(terminal_id)
    assert terminal["status"] == "deleted" and terminal["reconciliation_status"] == "verified"
    assert not owner._leases.leases_for_resource(terminal_id)
    original = await case.client.get(stream_path, params={"thread_id": thread_id})
    assert original.status_code == 200 and original.json()["data"] == frozen_stream
    restored = storage.read_items(session_id, thread_id, item_ids=tuple(item.item_id for item in items))
    assert canonical_json_bytes([item.to_dict() for item in restored]) == frozen_items
    previous_job = await case.client.get(f"/api/v1/jobs/{job_id}")
    assert previous_job.status_code == 200 and previous_job.json()["data"] == failed_job
    assert case.call_count(session_id, thread_id) == 3
    for assembly in case.container.checkpointer._storage.assemblies.list_context_assemblies(session_id, thread_id):
        assert (assembly.session_id, assembly.thread_id) == (session_id, thread_id)
        assert isinstance(assembly.model_call_id, str)
        assert assembly.request_hash_preimage == normalize_wire_request(case.request(assembly.model_call_id))
        assembly.validate_hashes()
    case.write_actual("native-cwd-failure", {"job": failed_job, "stream": frozen_stream,
        "tool_result": result.payload, "recovered": executed, "calls": [first_call, second, final]})
