from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.custom_tools import CustomToolFactoryContext


class ReadSessionRecentTextMessagesInput(BaseModel):
    """读取另一个会话最近文本消息的参数。"""

    session_id: str = Field(description="要读取的目标会话 ID。")
    rounds: int = Field(
        default=5,
        ge=1,
        le=50,
        description="最近用户轮次数，默认 5。",
    )


class GrepSessionContextJsonlInput(BaseModel):
    """搜索另一个会话当前有效上下文 JSONL 的参数。"""

    session_id: str = Field(description="要搜索的目标会话 ID。")
    pattern: str = Field(description="Python 正则表达式。")
    case_sensitive: bool = Field(default=False, description="是否区分大小写。")
    max_matches: int = Field(default=20, ge=1, le=200, description="最多返回的匹配行数。")
    expected_snapshot_id: str | None = Field(
        default=None,
        description="上一步返回的 snapshot_id；用于检测上下文是否已变化。",
    )


class ReadSessionContextJsonlInput(BaseModel):
    """按行读取另一个会话当前有效上下文 JSONL 的参数。"""

    session_id: str = Field(description="要读取的目标会话 ID。")
    line_start: int = Field(default=1, ge=1, description="起始行号，从 1 开始。")
    line_count: int = Field(default=20, ge=1, le=200, description="最多读取的行数。")
    max_chars_per_line: int = Field(
        default=4000,
        ge=200,
        le=20000,
        description="每行最多返回字符数，避免单条工具记录占用过多上下文。",
    )
    expected_snapshot_id: str | None = Field(
        default=None,
        description="上一步返回的 snapshot_id；用于检测上下文是否已变化。",
    )


@dataclass(frozen=True, slots=True)
class _SessionContextSnapshot:
    session_id: str
    snapshot_id: str
    content_sha256: str
    generated_at: str
    records: list[dict[str, object]]
    lines: list[str]
    byte_count: int
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None
    history_file_path: str | None


async def _load_context_snapshot(
    context: CustomToolFactoryContext,
    session_id: str,
) -> _SessionContextSnapshot:
    target_session_id = session_id.strip()
    if not target_session_id:
        raise ValueError("session_id 不能为空")

    await context.session_service.get(target_session_id)
    state = await context.message_service.get_agent_context_state(target_session_id)
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in state["records"]
    ]
    jsonl = "\n".join(lines)
    encoded = jsonl.encode("utf-8")
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    checkpoint_id = state["checkpoint_id"].strip()
    snapshot_id = checkpoint_id or f"content:{content_sha256}"
    return _SessionContextSnapshot(
        session_id=target_session_id,
        snapshot_id=snapshot_id,
        content_sha256=content_sha256,
        generated_at=datetime.now(UTC).isoformat(),
        records=state["records"],
        lines=lines,
        byte_count=len(encoded),
        raw_message_count=state["raw_message_count"],
        compacted=state["compacted"],
        compaction_cutoff=state["compaction_cutoff"],
        history_file_path=state["history_file_path"],
    )


def _snapshot_metadata(
    snapshot: _SessionContextSnapshot,
    expected_snapshot_id: str | None = None,
) -> dict[str, object]:
    if expected_snapshot_id is None:
        consistency = "not_checked"
        warning = None
    elif expected_snapshot_id == snapshot.snapshot_id:
        consistency = "matched"
        warning = None
    else:
        consistency = "changed"
        warning = (
            "目标 session 上下文已变化；当前结果来自新快照，"
            "不要与旧 grep/read 结果按行号拼接。"
        )
    return {
        "snapshot_id": snapshot.snapshot_id,
        "content_sha256": snapshot.content_sha256,
        "generated_at": snapshot.generated_at,
        "line_count": len(snapshot.lines),
        "raw_message_count": snapshot.raw_message_count,
        "byte_count": snapshot.byte_count,
        "compacted": snapshot.compacted,
        "compaction_cutoff": snapshot.compaction_cutoff,
        "history_file_path": snapshot.history_file_path,
        "expected_snapshot_id": expected_snapshot_id,
        "consistency": consistency,
        "warning": warning,
    }


def _content_text(content: object, *, assistant_text_blocks_only: bool) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if not assistant_text_blocks_only:
                parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if assistant_text_blocks_only and item_type != "text":
            continue
        if not assistant_text_blocks_only and item_type not in {None, "text", "input_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _is_user_record(record: dict[str, object]) -> bool:
    role = record.get("role")
    message_type = record.get("type")
    text = _content_text(record.get("content"), assistant_text_blocks_only=False)
    return (
        (role == "user" or message_type == "human")
        and bool(text)
        and not text.strip().startswith("<system_reminder>")
    )


def _is_assistant_record(record: dict[str, object]) -> bool:
    return record.get("role") == "assistant" or record.get("type") == "ai"


def _select_recent_user_rounds_with_assistant_text(
    records: list[dict[str, object]],
    rounds: int,
) -> list[dict[str, str]]:
    user_indexes = [
        index for index, record in enumerate(records)
        if _is_user_record(record)
    ]
    if not user_indexes:
        return []

    start_index = user_indexes[-rounds] if len(user_indexes) > rounds else user_indexes[0]
    selected: list[dict[str, str]] = []
    for record in records[start_index:]:
        if _is_user_record(record):
            text = _content_text(record.get("content"), assistant_text_blocks_only=False)
            selected.append({"role": "user", "text": text})
            continue
        if not _is_assistant_record(record):
            continue
        if record.get("tool_calls"):
            continue
        text = _content_text(record.get("content"), assistant_text_blocks_only=True)
        if text:
            selected.append({"role": "assistant", "type": "text", "text": text})
    return selected


def create_read_session_recent_text_messages_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建读取另一个 session 最近用户轮次和模型文本消息的扩展工具。"""

    async def read_session_recent_text_messages(
        session_id: str,
        rounds: int = 5,
    ) -> str:
        target_session_id = session_id.strip()
        if not target_session_id:
            raise ValueError("session_id 不能为空")

        snapshot = await _load_context_snapshot(context, target_session_id)
        messages = _select_recent_user_rounds_with_assistant_text(
            snapshot.records,
            rounds,
        )
        user_message_count = sum(1 for message in messages if message["role"] == "user")
        payload: dict[str, Any] = {
            "session_id": target_session_id,
            "rounds": rounds,
            "user_message_count": user_message_count,
            "context_snapshot": _snapshot_metadata(snapshot),
            "messages": messages,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool.from_function(
        coroutine=read_session_recent_text_messages,
        name="read_session_recent_text_messages",
        description=(
            "读取另一个 session 的 Agent State messages，返回最近 N 轮用户消息，"
            "以及这些轮次之间的模型 text 消息。默认 N=5。"
        ),
        args_schema=ReadSessionRecentTextMessagesInput,
    )


def create_grep_session_context_jsonl_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建搜索另一个 session 当前有效上下文 JSONL 的扩展工具。"""

    async def grep_session_context_jsonl(
        session_id: str,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> str:
        if not pattern:
            raise ValueError("pattern 不能为空")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(pattern, flags)
        except re.error as error:
            raise ValueError(f"pattern 不是有效正则表达式: {error}") from error

        snapshot = await _load_context_snapshot(context, session_id)
        matches: list[dict[str, object]] = []
        total_matching_lines = 0
        for index, line in enumerate(snapshot.lines, start=1):
            match = expression.search(line)
            if match is None:
                continue
            total_matching_lines += 1
            if len(matches) >= max_matches:
                continue
            preview_start = max(0, match.start() - 180)
            preview_end = min(len(line), match.end() + 180)
            matches.append(
                {
                    "line_number": index,
                    "match_start": match.start() + 1,
                    "match_end": match.end(),
                    "preview": line[preview_start:preview_end],
                    "preview_truncated_left": preview_start > 0,
                    "preview_truncated_right": preview_end < len(line),
                    "line_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                }
            )

        payload = {
            "session_id": snapshot.session_id,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "context_snapshot": _snapshot_metadata(
                snapshot,
                expected_snapshot_id,
            ),
            "total_matching_lines": total_matching_lines,
            "returned_match_count": len(matches),
            "matches_truncated": total_matching_lines > len(matches),
            "matches": matches,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool.from_function(
        coroutine=grep_session_context_jsonl,
        name="grep_session_context_jsonl",
        description=(
            "像 grep 文件一样用正则搜索另一个 session 的当前有效模型上下文 JSONL。"
            "可传 expected_snapshot_id 检测 grep 前后上下文变化。"
        ),
        args_schema=GrepSessionContextJsonlInput,
    )


def create_read_session_context_jsonl_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建按行读取另一个 session 当前有效上下文 JSONL 的扩展工具。"""

    async def read_session_context_jsonl(
        session_id: str,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
    ) -> str:
        snapshot = await _load_context_snapshot(context, session_id)
        start_index = min(line_start - 1, len(snapshot.lines))
        selected = snapshot.lines[start_index:start_index + line_count]
        lines = []
        for offset, line in enumerate(selected):
            clipped = line[:max_chars_per_line]
            lines.append(
                {
                    "line_number": start_index + offset + 1,
                    "text": clipped,
                    "original_chars": len(line),
                    "truncated": len(clipped) < len(line),
                    "line_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                }
            )
        line_end = start_index + len(selected)
        payload = {
            "session_id": snapshot.session_id,
            "context_snapshot": _snapshot_metadata(
                snapshot,
                expected_snapshot_id,
            ),
            "line_start": line_start,
            "line_end": line_end,
            "has_more": line_end < len(snapshot.lines),
            "next_line_start": line_end + 1 if line_end < len(snapshot.lines) else None,
            "lines": lines,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool.from_function(
        coroutine=read_session_context_jsonl,
        name="read_session_context_jsonl",
        description=(
            "像 read 文件一样按行读取另一个 session 的当前有效模型上下文 JSONL。"
            "可传 expected_snapshot_id 检测上次 grep/read 后上下文是否变化。"
        ),
        args_schema=ReadSessionContextJsonlInput,
    )
