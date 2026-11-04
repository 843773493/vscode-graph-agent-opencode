from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from app.schemas.event import Event

from .trace_cursor import (
    TraceCursorGoneError,
    _read_and_verify_line,
    event_line_digest,
    event_line_sample,
)
from .trace_index_compaction import _AnyEvent

TRACE_PAGE_MAX_BYTES = 512 * 1024
_TRACE_PAGE_CURSOR_PREFIX = "tp1."


class TracePageBudgetExceededError(RuntimeError):
    """单条 Trace 事件超过诊断页固定读取预算。"""


class TracePageBoundaryError(RuntimeError):
    """指定读取上界没有落在完整 JSONL 事件边界。"""


@dataclass(frozen=True, slots=True)
class TraceEventRecord:
    event: Event
    start: int
    end: int
    line: bytes


@dataclass(frozen=True, slots=True)
class TraceEventPage:
    records: list[TraceEventRecord]
    next_cursor: str | None
    has_more: bool
    bytes_read: int

    @property
    def events(self) -> list[Event]:
        return [record.event for record in self.records]


@dataclass(frozen=True, slots=True)
class _PageCursorPosition:
    line_start: int
    line_end: int
    line_digest: str


def _encode_page_cursor(*, line_start: int, line_end: int, line: bytes) -> str:
    raw = json.dumps(
        {
            "s": line_start,
            "n": line_end,
            "h": event_line_digest(len(line), event_line_sample(line)),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return _TRACE_PAGE_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode(
        "ascii"
    ).rstrip("=")


def _decode_page_cursor(cursor: str) -> _PageCursorPosition:
    if not cursor.startswith(_TRACE_PAGE_CURSOR_PREFIX):
        raise ValueError("Trace page cursor 前缀无效")
    try:
        encoded = cursor.removeprefix(_TRACE_PAGE_CURSOR_PREFIX)
        value = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        position = _PageCursorPosition(
            line_start=value["s"],
            line_end=value["n"],
            line_digest=value["h"],
        )
    except Exception as error:
        raise ValueError("Trace page cursor 格式无效") from error
    if (
        position.line_start < 0
        or position.line_end <= position.line_start
        or len(position.line_digest) != 32
    ):
        raise ValueError("Trace page cursor 字段无效")
    return position


def _validate_page_cursor(file: Path, cursor: str) -> int:
    position = _decode_page_cursor(cursor)
    if not file.is_file() or file.stat().st_size < position.line_end:
        raise ValueError("Trace page cursor 越过文件末尾")
    _read_and_verify_line(
        file,
        start=position.line_start,
        end=position.line_end,
        digest=position.line_digest,
        label="Trace page cursor",
    )
    return position.line_start


def read_trace_event_page(
    *,
    session_id: str,
    file: Path,
    cursor: str | None,
    limit: int,
    max_bytes: int = TRACE_PAGE_MAX_BYTES,
    before_offset: int | None = None,
) -> TraceEventPage:
    if limit < 1:
        raise ValueError("Trace page limit 必须大于 0")
    if max_bytes < 1:
        raise ValueError("Trace page max_bytes 必须大于 0")
    if not file.is_file() or file.stat().st_size == 0:
        if cursor is not None:
            raise TraceCursorGoneError(session_id, cursor)
        return TraceEventPage(records=[], next_cursor=None, has_more=False, bytes_read=0)
    try:
        page_end = (
            _validate_page_cursor(file, cursor)
            if cursor is not None
            else before_offset if before_offset is not None else file.stat().st_size
        )
        if not 0 <= page_end <= file.stat().st_size:
            raise ValueError("Trace 页边界越过文件范围")
    except ValueError as error:
        raise TraceCursorGoneError(session_id, cursor or "") from error
    if page_end == 0:
        return TraceEventPage(records=[], next_cursor=None, has_more=False, bytes_read=0)

    read_start = max(0, page_end - max_bytes)
    with file.open("rb") as stream:
        stream.seek(page_end - 1)
        if stream.read(1) != b"\n":
            raise TracePageBoundaryError("Trace 诊断页边界不是完整 JSONL 行")
        starts_at_boundary = read_start == 0
        if read_start > 0:
            stream.seek(read_start - 1)
            starts_at_boundary = stream.read(1) == b"\n"
        stream.seek(read_start)
        payload = stream.read(page_end - read_start)
    bytes_read = len(payload)
    if not starts_at_boundary:
        first_newline = payload.find(b"\n")
        if first_newline < 0:
            raise TracePageBudgetExceededError(
                f"Trace 单事件超过诊断页读取预算: max_bytes={max_bytes}"
            )
        read_start += first_newline + 1
        payload = payload[first_newline + 1 :]

    if not payload and read_start > 0:
        raise TracePageBudgetExceededError(
            f"Trace 单事件超过诊断页读取预算: max_bytes={max_bytes}"
        )
    selected_reversed: list[tuple[int, int, bytes]] = []
    line_end_offset = len(payload)
    while line_end_offset:
        line_content_end_offset = (
            line_end_offset - 1
            if payload[line_end_offset - 1] == ord("\n")
            else line_end_offset
        )
        line_start_offset = payload.rfind(b"\n", 0, line_content_end_offset) + 1
        line = payload[line_start_offset:line_end_offset]
        if line.strip():
            selected_reversed.append(
                (
                    read_start + line_start_offset,
                    read_start + line_end_offset,
                    line,
                )
            )
            if len(selected_reversed) == limit:
                break
        line_end_offset = line_start_offset
    selected = list(reversed(selected_reversed))
    records = [parse_trace_record(session_id, start, end, line) for start, end, line in selected]
    if not selected:
        return TraceEventPage(
            records=[], next_cursor=None, has_more=False, bytes_read=bytes_read
        )
    oldest_line_start, oldest_line_end, oldest_line = selected[0]
    has_more = oldest_line_start > 0
    return TraceEventPage(
        records=records,
        next_cursor=(
            _encode_page_cursor(
                line_start=oldest_line_start,
                line_end=oldest_line_end,
                line=oldest_line,
            )
            if has_more
            else None
        ),
        has_more=has_more,
        bytes_read=bytes_read,
    )


def parse_trace_record(session_id: str, start: int, end: int, line: bytes) -> TraceEventRecord:
    try:
        event = _AnyEvent.model_validate_json(line).root
    except Exception as error:
        raise RuntimeError(
            "Trace 事件协议无效: "
            f"session_id={session_id}, offset={start}, line={line[:200]!r}"
        ) from error
    return TraceEventRecord(event=event, start=start, end=end, line=line)
