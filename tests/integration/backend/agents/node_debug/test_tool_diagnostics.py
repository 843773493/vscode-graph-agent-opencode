"""真实工具调用的失败结果与调试动作共享公开事实，并能原入口重试。"""

from __future__ import annotations

import errno
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
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
    source_case as source_case,  # noqa: PLC0414 - 使用当前文件的隔离工作区。
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 真实日志写入产生 ENOTDIR。
)


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_debug_tool_native_failure_keeps_canonical_action_and_same_thread_retry(
    source_case: RuntimeSourceCase,
    diagnostic_sink: DiagnosticFailure,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    log_fails: bool,
) -> None:
    case = source_case
    session, thread, _ = await _target(case)
    catalog = await case.client.get("/api/v1/tools", params={"agent_id": "default"})
    assert catalog.status_code == 200, catalog.text
    (tool,) = [
        item for item in catalog.json()["data"] if item["name"] == "add_breakpoint"
    ]
    selected = await case.client.patch(
        "/api/v1/tools/selection",
        json={
            "agent_id": "default",
            "changes": [
                {
                    "tool_id": tool["tool_id"],
                    "execution_enabled": True,
                    "model_visible": True,
                },
            ],
        },
    )
    assert selected.status_code == 200, selected.text
    source = case.workspace / "debug-entry.mjs"
    source.write_text("console.log('真实断点源文件');\n", encoding="utf-8")
    blocker = tmp_path / "debug-tool-secret"
    blocker.write_text("保留原生读取故障原件", encoding="utf-8")
    service = case.container.node_debug_service
    normalize = service.workspace_relative_path
    causes: list[OSError] = []

    def read_failure(raw_path: str, field_name: str) -> str:
        normalized = normalize(raw_path, field_name)
        if raw_path == source.name:
            try:
                (blocker / "entry.mjs").read_bytes()
            except OSError as error:
                causes.append(error)
                if log_fails:
                    diagnostic_sink.primary = error
                raise
        return normalized

    results = []
    first_items = ()
    first_bytes = b""
    original_rollout = b""
    storage = case.container.checkpointer._storage.storage
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    thread_root = resolver.resolve_thread_node(session, thread)
    root = thread_root / "rollout"
    for index in range(2):
        wire_id = f"debug-breakpoint-{index}"
        call, followup = await case.plan(
            session,
            [
                _tool_reply(
                    "invoke_extension_tool",
                    {
                        "tool_name": "add_breakpoint",
                        "arguments": {"fileFullPath": source.name, "line": 1},
                    },
                    wire_id,
                ),
                {"content": f"断点调用已确认-{index}"},
            ],
        )
        with monkeypatch.context() as patch:
            if index == 0:
                patch.setattr(service, "workspace_relative_path", read_failure)
            accepted = await _send(
                case, session, f"在源文件第一行设置断点，第{index}次。"
            )
            assert accepted["thread_id"] == thread
            await _job(case, str(accepted["job_id"]))
        operation = call + ":tool-call:" + wire_id
        result = case.tool_result(operation)
        response = await case.client.get(
            "/api/v1/debug/node", params={"session_id": session, "thread_id": thread}
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        state = response.json()["data"]
        (action,) = [
            item for item in state["actions"] if item["tool_call_id"] == wire_id
        ]
        assert action["tool_name"] == "add_breakpoint" and action["actor"] == "ai"
        assert action["extension_catalog_binding"]["target_id"] == tool["name"]
        if index == 0:
            expected_message = (
                f"执行系统 I/O 失败: error=NotADirectoryError, errno={errno.ENOTDIR}, "
                f"reason={causes[0].strerror}, filename={str(blocker / 'entry.mjs')!r}"
            )
            if log_fails:
                expected_message += ", diagnostic_sink_error=NotADirectoryError"
            assert result["ok"] is False
            assert result["error"] == {
                "code": "DEBUG_TOOL_FAILED",
                "message": expected_message,
            }
            assert action["result"] == "error" and action["message"] == expected_message
            assert not state["breakpoints"]
        else:
            assert result["ok"] is True and action["result"] == "success"
            assert len(state["breakpoints"]) == 1
        items = storage.read_items(session, thread)
        (item,) = [
            item
            for item in items
            if item.semantic_kind == "tool_result"
            and isinstance(item.payload, Mapping)
            and item.payload["tool_call_id"] == operation
        ]
        assert (
            isinstance(item.payload, Mapping)
            and json.loads(item.payload["content"]) == result
        )
        messages = case.request(followup)["messages"]
        assert isinstance(messages, list)
        (tool_message,) = [
            message
            for message in messages
            if message["role"] == "tool" and message["tool_call_id"] == wire_id
        ]
        assert tool_message["content"] == item.payload["content"]
        (paired,) = [
            tool
            for message in messages
            for tool in message.get("tool_calls", [])
            if tool["id"] == wire_id
        ]
        assert paired["function"]["name"] == "invoke_extension_tool"
        results.append(
            {
                "result": result,
                "action": action,
                "state": state,
                "tool_item": item.to_dict(),
            }
        )
        if index == 0:
            first_items = items
            first_bytes = canonical_json_bytes([item.to_dict() for item in items])
            original_rollout = (root / "rollout.jsonl").read_bytes()

    (cause,) = causes
    assert cause.errno == errno.ENOTDIR and "debug-tool-secret" in str(cause)
    if log_fails:
        (error,) = diagnostic_sink.errors
        assert error.errno == errno.ENOTDIR
        assert any(str(error) in note for note in cause.__notes__)
        assert all(
            record.exc_info and record.exc_info[1] is cause
            for record in diagnostic_sink.records
        )
    else:
        assert not diagnostic_sink.errors and not getattr(cause, "__notes__", [])
    old_items = storage.read_items(
        session, thread, item_ids=tuple(item.item_id for item in first_items)
    )
    assert canonical_json_bytes([item.to_dict() for item in old_items]) == first_bytes
    assert (root / "rollout.jsonl").read_bytes().startswith(original_rollout)
    assemblies = (
        case.container.checkpointer._storage.assemblies.list_context_assemblies(
            session, thread
        )
    )
    assert {assembly.model_call_id for assembly in assemblies} == set(
        case.planned_calls
    )
    for assembly in assemblies:
        assert (assembly.session_id, assembly.thread_id) == (session, thread)
        assert isinstance(assembly.model_call_id, str)
        assert assembly.request_hash_preimage == normalize_wire_request(
            case.request(assembly.model_call_id)
        )
        assembly.validate_hashes()
    public = json.dumps([results, case.requests()], ensure_ascii=False)
    assert "debug-tool-secret" in public and "diagnostic-log-secret" not in public
    with storage._connect(thread, session_id=session, read_only=True) as connection:
        persisted = "\n".join(connection.iterdump())
    assert (
        "debug-tool-secret" in persisted
        and "diagnostic-log-secret" not in persisted
    )
    content = (thread_root / "debug/node/manifest.json").read_text(encoding="utf-8")
    assert "debug-tool-secret" in content and "diagnostic-log-secret" not in content
    manifest = json.loads(content)
    for result in results:
        (action,) = [
            item
            for item in manifest["actions"]
            if item["tool_call_id"] == result["action"]["tool_call_id"]
        ]
        assert action == result["action"]
    case.write_actual(
        f"debug-tool-failure-{log_fails}",
        {"results": results, "calls": list(case.planned_calls)},
    )
