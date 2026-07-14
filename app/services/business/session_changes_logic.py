from __future__ import annotations

import difflib
import hashlib
import time
from collections.abc import Callable

from app.abstractions.session_changes import AggregatedFileChange, StoredFileEdit
from app.schemas.public_v2.session_changes import (
    SessionChangesetKind,
    SessionFileChangeKind,
)


TEXT_PREVIEW_LIMIT = 8000


def build_edit_id(*, turn_id: str, tool_call_id: str, file_path: str) -> str:
    value = f"{turn_id}\0{tool_call_id}\0{file_path}\0{time.time_ns()}"
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
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
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
    read_relative_text: Callable[[str | None], str],
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
            read_relative_text(first.before_file)
            if first.before_exists and first.before_file
            else None
        )
        after_content = (
            read_relative_text(last.after_file)
            if last.after_exists and last.after_file
            else None
        )
        changes.append(
            AggregatedFileChange(
                file_path=file_path,
                kind=detect_change_kind(
                    before_exists=before_content is not None,
                    after_exists=after_content is not None,
                ),
                additions=sum(record.additions for record in ordered),
                deletions=sum(record.deletions for record in ordered),
                latest_edit_id=last.edit_id,
                tool_call_ids=tuple(
                    dict.fromkeys(record.tool_call_id for record in ordered)
                ),
                execution_ids=tuple(
                    dict.fromkeys(record.execution_id for record in ordered)
                ),
                turn_ids=tuple(dict.fromkeys(record.turn_id for record in ordered)),
                before_content=before_content,
                after_content=after_content,
            )
        )
    return sorted(changes, key=lambda item: item.file_path)


def sorted_turn_ids(records: list[StoredFileEdit]) -> list[str]:
    seen: dict[str, str] = {}
    for record in records:
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
        return "turn", changeset_id.removeprefix("turn:")
    raise ValueError(f"不支持的 changeset_id: {changeset_id}")


def preview(content: str | None) -> str | None:
    if content is None or len(content) <= TEXT_PREVIEW_LIMIT:
        return content
    return (
        f"{content[:TEXT_PREVIEW_LIMIT]}\n\n"
        "[内容过长，已截断预览；完整内容请打开 changes 目录中的可读文件。]"
    )
