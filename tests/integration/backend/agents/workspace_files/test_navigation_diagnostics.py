"""真实文件读取失败的诊断次错不能改变 canonical 工具结果。"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.core.resource_uri import resource_display_uri
from app.domain.itemized.hashing import canonical_json_bytes
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
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离工作区与产物。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_file_read_failure_keeps_paired_history_and_same_thread_retry(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, tmp_path: Path, log_fails: bool,
) -> None:
    case = source_case
    session = await create_session(case.client, "文件读取失败与原会话恢复")
    thread = await case.container.session_service.resolve_main_thread(session)
    source = case.workspace / "指南 .boxteam-notes read_file_path %40.txt"
    source.write_bytes("只由真实文件读取返回的原正文 Ω\r\n".encode())
    original = source.read_bytes()
    uri = resource_display_uri(scope="workspace", scope_id=case.container.workspace_id,
                               kind="files", tail_segments=("root", source.name))
    blocker = tmp_path / "native-read-secret"
    blocker.write_text("原生读取故障", encoding="utf-8")
    log_blocker = tmp_path / "navigation-log-secret"
    log_blocker.write_text("原生日志故障", encoding="utf-8")
    open_file, capture = Path.open, caplog.handler.emit
    causes: list[OSError] = []
    sink_errors: list[OSError] = []
    logged: list[BaseException] = []

    def fail_open(path: Path, *args, **kwargs):
        if path == source and args == ("rb",):
            try:
                return open_file(blocker / "guide.txt", *args, **kwargs)
            except OSError as error:
                causes.append(error)
                raise
        return open_file(path, *args, **kwargs)

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if not record.getMessage().startswith("文件导航"):
            return
        assert record.exc_info is not None and isinstance(record.exc_info[1], BaseException)
        assert record.exc_info[1] is causes[-1]
        logged.append(record.exc_info[1])
        if log_fails:
            try:
                (log_blocker / "service.log").write_text(record.getMessage(), encoding="utf-8")
            except OSError as error:
                sink_errors.append(error)
                raise

    saver = case.container.checkpointer
    storage = saver._storage.storage
    results = []
    first_items = ()
    first_bytes = b""
    first_canonical = b""
    frozen = {}
    for index in range(2):
        wire_id = f"read-guide-{index}"
        call, followup = await case.plan(session, [
            _tool_reply("read_file", {"path": uri}, wire_id),
            {"content": f"文件工具结果已确认-{index}"},
        ])
        with monkeypatch.context() as patch:
            if index == 0:
                patch.setattr(Path, "open", fail_open)
                patch.setattr(caplog.handler, "emit", emit)
            accepted = await _send(case, session, f"读取指定资源，第{index}次。")
            assert accepted["thread_id"] == thread
            await _job(case, str(accepted["job_id"]))
        operation = call + ":tool-call:" + wire_id
        items = storage.read_items(session, thread)
        item, = [item for item in items if item.semantic_kind == "tool_result"
                 and isinstance(item.payload, Mapping) and item.payload["tool_call_id"] == operation]
        assert isinstance(item.payload, Mapping)
        content = item.payload["content"]
        assert isinstance(content, str)
        messages = case.request(followup)["messages"]
        assert isinstance(messages, list)
        tool_message, = [message for message in messages if message["role"] == "tool" and message["tool_call_id"] == wire_id]
        assert tool_message["content"] == content
        paired, = [tool for message in messages for tool in message.get("tool_calls", []) if tool["id"] == wire_id]
        assert paired["function"]["name"] == "read_file" and json.loads(paired["function"]["arguments"]) == {"path": uri}
        if index == 0:
            detail = json.loads(content)
            expected_message = "文件导航文件系统访问失败"
            if log_fails:
                expected_message += ", diagnostic_sink_error=NotADirectoryError"
            assert detail == {"owner": "resource_file_operations", "code": "execution_failed",
                "reason": "filesystem", "type": "NotADirectoryError", "errno": errno.ENOTDIR,
                "message": expected_message}
            assert item.payload["tool_outcome"] == "failure"
            first_items = items
            first_bytes = canonical_json_bytes([item.to_dict() for item in items])
            first_canonical = storage.jsonl_path(session, thread_id=thread).read_bytes()
            frozen = {assembly.assembly_id: assembly.to_dict() for assembly in saver.list_context_assemblies(session, thread)}
        else:
            assert item.payload["tool_outcome"] == "success" and "只由真实文件读取返回的原正文 Ω" in content
            assert item.payload["execution_confirmed"] is True
        assert source.read_bytes() == original
        results.append(item.to_dict())
    cause, = causes
    assert cause.errno == errno.ENOTDIR and cause.filename == str(blocker / "guide.txt")
    assert logged == [cause]
    if log_fails:
        sink_error, = sink_errors
        assert sink_error.errno == errno.ENOTDIR
        assert any(str(sink_error) in note for note in cause.__notes__)
    else:
        assert not sink_errors and not getattr(cause, "__notes__", [])
    retained = storage.read_items(session, thread, item_ids=tuple(item.item_id for item in first_items))
    assert canonical_json_bytes([item.to_dict() for item in retained]) == first_bytes
    canonical = storage.jsonl_path(session, thread_id=thread).read_bytes()
    assert canonical.startswith(first_canonical)
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        for assembly_id, original_assembly in frozen.items():
            restored = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
            assert restored.to_dict() == original_assembly
            restored.validate_hashes()
    index = storage.index_path(session, "", thread_id=thread)
    session_root = case.container.session_service.path_resolver.resolve_session_node(session)
    captures = {call: json.loads((case.model.artifacts / (call + ".json")).read_text()) for call in case.planned_calls}
    proof = audit_sealed_requests(index, (session_root / "logs/llm_requests").glob("*.json"), captures)
    assert len(case.requests()) == 4
    with storage._connect(thread, session_id=session, read_only=True) as connection:
        persisted = "\n".join(connection.iterdump())
    for native in (str(case.workspace), str(blocker), str(log_blocker)):
        assert native not in json.dumps([results, case.requests()], ensure_ascii=False)
        assert native not in persisted and native.encode() not in canonical
    case.write_actual(f"file-navigation-diagnostics-{log_fails}", {
        "results": results, "sealed_requests": proof, "diagnostic_sink_errors": len(sink_errors),
        "scope": "真实ASGI消息/Agent/model_stream/SQLite及新Saver冷读；不代Gateway/Web、独立Backend重启或fork验收",
    })
