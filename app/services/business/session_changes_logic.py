from __future__ import annotations

import difflib
import hashlib
import re
import time
from collections.abc import Callable
from typing import Literal

from app.abstractions.session_changes import AggregatedFileChange, StoredFileEdit
from app.schemas.internal_v2.session_changes import (
    SessionChangesetKind,
    SessionFileChangeKind,
)

SESSION_FILE_CHANGE_PREVIEW_LIMIT = 8000
_DIFF_LINE_BREAK = re.compile(
    r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]"
)
_NON_LF_LINE_BREAK = re.compile(r"[\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_DIFF_LINE_BREAKS = (
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


def build_edit_id(
    *,
    turn_id: str | None,
    execution_id: str,
    tool_call_id: str,
    file_path: str,
) -> str:
    owner_id = turn_id if turn_id is not None else execution_id
    value = f"{owner_id}\0{tool_call_id}\0{file_path}\0{time.time_ns()}"
    return f"edit_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


def build_unified_diff(
    *,
    file_path: str,
    before_content: str,
    after_content: str,
) -> str:
    return "".join(
        difflib.unified_diff(
            before_content.splitlines(keepends=True),
            after_content.splitlines(keepends=True),
            fromfile=f"a{file_path}",
            tofile=f"b{file_path}",
        )
    )


def count_diff_lines(diff_text: str) -> tuple[int, int]:
    # unified_diff 只在最前面固定输出 "---" / "+++" 两行文件头，hunk 之后
    # 的所有 +/- 行都是真实变更。不能按前缀识别文件头：正文本身以 ++ 或
    # -- 开头时会被误删，导致该类文件的 additions/deletions 恒为 0。
    body_start = 0
    for _ in range(2):
        line_break = _DIFF_LINE_BREAK.search(diff_text, body_start)
        if line_break is None:
            return 0, 0
        body_start = line_break.end()

    additions = int(diff_text.startswith("+", body_start))
    deletions = int(diff_text.startswith("-", body_start))
    if _NON_LF_LINE_BREAK.search(diff_text, body_start) is None:
        additions += diff_text.count("\n+", body_start)
        deletions += diff_text.count("\n-", body_start)
        return additions, deletions

    for line_break in _DIFF_LINE_BREAKS:
        additions += diff_text.count(f"{line_break}+", body_start)
        deletions += diff_text.count(f"{line_break}-", body_start)
    return additions, deletions


def detect_change_kind(*, before_exists: bool, after_exists: bool) -> SessionFileChangeKind:
    if not before_exists and after_exists:
        return "create"
    if before_exists and not after_exists:
        return "delete"
    return "edit"


def aggregate_records(
    records: list[StoredFileEdit],
    *,
    read_edit_text: Callable[[str, Literal["before", "after"]], str],
) -> list[AggregatedFileChange]:
    grouped: dict[str, list[StoredFileEdit]] = {}
    for record in records:
        grouped.setdefault(record.file_path, []).append(record)

    changes: list[AggregatedFileChange] = []
    for file_path, file_records in grouped.items():
        ordered = sorted(file_records, key=lambda item: item.created_at)
        first = ordered[0]
        last = ordered[-1]
        before_content = (
            read_edit_text(first.edit_id, "before")
            if first.before_exists
            else None
        )
        after_content = (
            read_edit_text(last.edit_id, "after")
            if last.after_exists
            else None
        )
        changes.append(
            AggregatedFileChange(
                file_path=file_path,
                kind=detect_change_kind(
                    before_exists=before_content is not None,
                    after_exists=after_content is not None,
                ),
                latest_edit_id=last.edit_id,
                tool_call_ids=tuple(
                    dict.fromkeys(record.tool_call_id for record in ordered)
                ),
                execution_ids=tuple(
                    dict.fromkeys(record.execution_id for record in ordered)
                ),
                turn_ids=tuple(
                    dict.fromkeys(
                        record.turn_id
                        for record in ordered
                        if record.turn_id is not None
                    )
                ),
                before_content=before_content,
                after_content=after_content,
            )
        )
    return sorted(changes, key=lambda item: item.file_path)


def sorted_turn_ids(records: list[StoredFileEdit]) -> list[str]:
    seen: dict[str, str] = {}
    for record in records:
        if record.turn_id is not None:
            seen.setdefault(record.turn_id, record.created_at)
    return [
        turn_id
        for turn_id, _ in sorted(seen.items(), key=lambda item: item[1], reverse=True)
    ]


def changeset_kind_and_turn(
    changeset_id: str,
) -> tuple[SessionChangesetKind, str | None]:
    if changeset_id == "all":
        return "all", None
    if changeset_id.startswith("turn:"):
        turn_id = changeset_id.removeprefix("turn:")
        if not turn_id:
            raise ValueError("turn changeset 缺少 turn_id")
        return "turn", turn_id
    raise ValueError(f"不支持的 changeset_id: {changeset_id}")


def changeset_records(
    records: list[StoredFileEdit],
    changeset_id: str,
) -> list[StoredFileEdit]:
    """按 changeset_id 唯一解析并筛选记录，供列表与详情共用。"""
    kind, turn_id = changeset_kind_and_turn(changeset_id)
    if kind == "all":
        return records
    return [record for record in records if record.turn_id == turn_id]


def preview_session_file_content(file_content: str | None) -> str | None:
    if file_content is None or len(file_content) <= SESSION_FILE_CHANGE_PREVIEW_LIMIT:
        return file_content
    return (
        f"{file_content[:SESSION_FILE_CHANGE_PREVIEW_LIMIT]}\n\n"
        "[内容过长，已截断预览；完整内容请打开 changes 目录中的可读文件。]"
    )
