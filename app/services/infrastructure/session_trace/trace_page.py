from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass

from pydantic import RootModel

from app.schemas.event import Event

from .trace_cursor import (
    TraceCursorGoneError,
    _read_and_verify_line,
    event_line_digest,
    event_line_sample,
)


class _AnyEvent(RootModel[Event]):
    pass


TRACE_PAGE_MAX_BYTES = 512 * 1024
_TRACE_PAGE_CURSOR_PREFIX = "tp1."


class TracePageBudgetExceededError(RuntimeError):
    """单条 Trace 事件超过诊断页固定读取预算。"""


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


def read_trace_event_page(
    *,
    session_id: str,
    connection: sqlite3.Connection | None,
    cursor: str | None,
    limit: int,
    max_bytes: int = TRACE_PAGE_MAX_BYTES,
    before_offset: int | None = None,
    thread_id: str | None = None,
) -> TraceEventPage:
    if limit < 1 or max_bytes < 1:
        raise ValueError("Trace page limit 和 max_bytes 必须大于 0")
    tail = (
        connection.execute(
            "SELECT end FROM trace_events ORDER BY start DESC LIMIT 1"
        ).fetchone()
        if connection is not None
        else None
    )
    size = tail[0] if tail else 0
    try:
        if cursor is not None:
            position = _decode_page_cursor(cursor)
            _read_and_verify_line(
                connection,
                start=position.line_start,
                end=position.line_end,
                digest=position.line_digest,
                label="Trace page cursor",
            )
            before = position.line_start
        else:
            before = before_offset if before_offset is not None else size
        if before and (
            connection is None
            or connection.execute(
                "SELECT 1 FROM trace_events WHERE end=?", (before,)
            ).fetchone()
            is None
        ):
            raise ValueError("Trace page 边界不在已提交事件末尾")
        if before < 0 or before > size:
            raise ValueError("Trace page 边界越界")
    except ValueError as error:
        raise TraceCursorGoneError(session_id, cursor or "") from error
    selected: list[TraceEventRecord] = []
    bytes_read = 0
    scope = " AND thread_id=?" if thread_id is not None else ""
    if connection is not None:
        parameters = (before, thread_id, limit) if thread_id is not None else (before, limit)
        boundaries = connection.execute(
            f"SELECT start,end FROM trace_events WHERE start<?{scope} ORDER BY start DESC LIMIT ?",
            parameters,
        )
        for start, end in boundaries:
            if bytes_read + end - start > max_bytes:
                if not selected:
                    raise TracePageBudgetExceededError(
                        f"Trace 单事件超过诊断页读取预算: max_bytes={max_bytes}"
                    )
                break
            raw = (
                connection.execute(
                    "SELECT raw FROM trace_events WHERE start=?", (start,)
                )
                .fetchone()[0]
                .encode("utf-8")
            )
            selected.append(parse_trace_record(session_id, start, end, raw))
            bytes_read += len(raw)
    selected.reverse()
    oldest = selected[0] if selected else None
    has_more = bool(oldest is not None and connection is not None and connection.execute(
        f"SELECT 1 FROM trace_events WHERE start<?{scope} LIMIT 1",
        (oldest.start, thread_id) if thread_id is not None else (oldest.start,),
    ).fetchone())
    return TraceEventPage(
        records=selected,
        next_cursor=(
            _encode_page_cursor(
                line_start=oldest.start, line_end=oldest.end, line=oldest.line
            )
            if has_more
            else None
        ),
        has_more=has_more,
        bytes_read=bytes_read,
    )


def parse_trace_record(
    session_id: str, start: int, end: int, line: bytes
) -> TraceEventRecord:
    try:
        event = _AnyEvent.model_validate_json(line).root
    except Exception as error:
        raise RuntimeError(
            "Trace 事件协议无效: "
            f"session_id={session_id}, offset={start}, line={line[:200]!r}"
        ) from error
    return TraceEventRecord(event=event, start=start, end=end, line=line)
