"""工具结束事件的执行状态、可信引用和文件变更收口输入。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.abstractions.session_changes import (
    FileEditSnapshot,
    SessionChangesRecorderProtocol,
    StoredFileEdit,
)
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tools.apply_patch import (
    APPLY_PATCH_TOOL_NAME,
    load_apply_patch_journal_from_result,
)
from app.services.orchestration.agent_stream_helpers import (
    extract_tool_result_text,
    normalize_tool_args,
)
from app.services.orchestration.event_stream.contracts import ToolEventDisplayContext

FILE_EDIT_TOOL_NAMES = {"write_file", "edit_file", APPLY_PATCH_TOOL_NAME}
SUBAGENT_TOOL_NAMES = frozenset({"task"})


@dataclass(frozen=True, slots=True)
class ResourceActivityBinding:
    """由实际资源 owner 附带到工具事件的已确认资源身份。"""

    resource_id: str


def resource_activity_binding_from_metadata(
    metadata: object,
) -> ResourceActivityBinding | None:
    """解析 typed owner event；绝不从工具参数猜测资源。"""
    if not isinstance(metadata, Mapping) or "resource_activity" not in metadata:
        return None
    raw_binding = metadata["resource_activity"]
    if not isinstance(raw_binding, Mapping):
        raise TypeError("resource_activity 必须是对象")
    unknown = set(raw_binding) - {"resource_id"}
    if unknown:
        raise TypeError(f"resource_activity 含未知字段: {sorted(unknown)}")
    resource_id = raw_binding.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise TypeError("resource_activity.resource_id 必须是非空字符串")
    return ResourceActivityBinding(resource_id=resource_id.strip())


def tool_message_from_output(
    output: object,
    *,
    execution_id: str,
    tool_name: str,
) -> ToolMessage:
    if isinstance(output, ToolMessage):
        return output
    if isinstance(output, Command):
        update = output.update
        messages = update.get("messages") if isinstance(update, Mapping) else None
        if isinstance(messages, Sequence) and not isinstance(
            messages, (str, bytes, bytearray)
        ):
            tool_messages = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if len(tool_messages) == 1:
                return tool_messages[0]
            raise TypeError(
                "工具结束事件的 Command 必须包含且只包含一个 ToolMessage: "
                f"execution_id={execution_id} tool={tool_name} "
                f"tool_message_count={len(tool_messages)}"
            )
    raise TypeError(
        "工具结束事件必须返回带 tool_call_id 的 ToolMessage，或包含该消息的 Command: "
        f"execution_id={execution_id} tool={tool_name} "
        f"output_type={type(output).__name__}"
    )


def declared_tool_payload_status(parsed: Mapping[str, Any]) -> str | None:
    """解析工具返回体中显式声明的执行状态，未声明时返回 None。

    工作区工具结果用两种显式声明表达同一事实：`status` 取 error/success，
    `ok` 取布尔值。二者都是可信的执行结论，缺一不可；只认其中一种会把
    另一种显式失败静默当成成功。
    """
    result_status = parsed.get("status")
    if result_status == "error":
        return "error"
    if result_status == "success":
        return "success"
    result_ok = parsed.get("ok")
    if isinstance(result_ok, bool):
        return "success" if result_ok else "error"
    return None


def tool_output_status(output: Any) -> str:
    """合并 ToolMessage 状态与工具返回体中的状态。"""
    status = getattr(output, "status", None)
    if status == "error":
        return "error"
    text = extract_tool_result_text(output).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        payload_status = declared_tool_payload_status(parsed)
        if payload_status is not None:
            return payload_status
    if status == "success":
        return "success"
    return "success" if text and not text.startswith("Error:") else "error"


def tool_output_succeeded(output: Any) -> bool:
    return tool_output_status(output) == "success"


def file_paths_from_tool_args(tool_name: str, tool_args: dict[str, Any]) -> list[str]:
    if tool_name not in FILE_EDIT_TOOL_NAMES:
        return []
    if tool_name == APPLY_PATCH_TOOL_NAME:
        return []
    value = tool_args.get("file_path")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    raise RuntimeError(f"{tool_name} 工具缺少 file_path，无法记录文件变更")


def stored_edit_payload(record: StoredFileEdit) -> dict[str, object]:
    return {
        "edit_id": record.edit_id,
        "file_path": record.file_path,
        "kind": record.kind,
        "additions": record.additions,
        "deletions": record.deletions,
        "diff_file": record.diff_file,
        "before_file": record.before_file,
        "after_file": record.after_file,
    }


def apply_patch_snapshots_from_result(
    *,
    result_text: str,
    session_changes_service: SessionChangesRecorderProtocol,
    workspace_root: Path,
) -> list[FileEditSnapshot]:
    snapshots: list[FileEditSnapshot] = []
    for raw_snapshot in load_apply_patch_journal_from_result(
        result_text,
        workspace_root=workspace_root,
    ):
        file_path = raw_snapshot.get("file_path")
        before_exists = raw_snapshot.get("before_exists")
        before_content = raw_snapshot.get("before_content")
        if not isinstance(file_path, str):
            raise TypeError("apply_patch journal 快照缺少 file_path")
        if not isinstance(before_exists, bool):
            raise TypeError(f"apply_patch journal 快照 {file_path} 缺少 before_exists")
        if not isinstance(before_content, str):
            raise TypeError(f"apply_patch journal 快照 {file_path} 缺少 before_content")
        snapshots.append(
            session_changes_service.build_snapshot(
                file_path=file_path,
                existed=before_exists,
                content=before_content,
            )
        )
    return snapshots


def build_tool_display_context(
    *,
    raw_tool_name: str,
    raw_tool_args: dict[str, Any],
) -> ToolEventDisplayContext:
    if raw_tool_name == EXTENSION_TOOL_INVOKER_NAME:
        target_tool_name = raw_tool_args.get("tool_name")
        if isinstance(target_tool_name, str) and target_tool_name.strip():
            return ToolEventDisplayContext(
                tool_name=target_tool_name.strip(),
                tool_args=normalize_tool_args(raw_tool_args.get("arguments")),
                invocation_tool_name=raw_tool_name,
            )

    return ToolEventDisplayContext(
        tool_name=raw_tool_name,
        tool_args=raw_tool_args,
        invocation_tool_name=None,
    )


def activity_result_detail(
    result_text: str,
    *,
    tool_name: str,
    agent_id: str,
) -> dict[str, object]:
    """从工具结果提取已允许进入 Activity detail 的最小事实。"""
    detail: dict[str, object] = {
        "phase": "completed",
        "agent_id": agent_id,
    }
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        if declared_tool_payload_status(parsed) == "error":
            detail["phase"] = "failed"
        code = parsed.get("code")
        if isinstance(code, str) and code:
            detail["code"] = code
        retryable = parsed.get("retryable")
        if isinstance(retryable, bool):
            detail["retryable"] = retryable
        recovery = parsed.get("recovery")
        if isinstance(recovery, str) and recovery:
            detail["recovery"] = recovery
        timeout_ms = parsed.get("timeoutMs")
        if (
            isinstance(timeout_ms, int)
            and not isinstance(timeout_ms, bool)
            and timeout_ms > 0
        ):
            detail["timeout_ms"] = timeout_ms
    if tool_name in SUBAGENT_TOOL_NAMES and isinstance(parsed, Mapping):
        child_thread_id = parsed.get("child_thread_id")
        if isinstance(child_thread_id, str) and child_thread_id:
            detail["child_turn_id"] = child_thread_id
    return detail
