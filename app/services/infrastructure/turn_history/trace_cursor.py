from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.schemas.event import Event

from .trace_index import TraceTurnIndex


class TraceCursorGoneError(RuntimeError):
    """请求的事件游标已不在当前 trace 文件中。"""

    def __init__(self, session_id: str, cursor: str) -> None:
        self.session_id = session_id
        self.cursor = cursor
        super().__init__(f"Trace 事件游标不存在: session_id={session_id}, cursor={cursor}")


@dataclass(frozen=True, slots=True)
class TraceStreamRecord:
    event: Event
    cursor: str


@dataclass(frozen=True, slots=True)
class _TraceCursorPosition:
    event_id: str
    start: int
    end: int
    digest: str


_TRACE_CURSOR_PREFIX = "tc1."
_CURSOR_SAMPLE_BYTES = 256


def _cursor_digest(line: bytes) -> str:
    sample = (
        line
        if len(line) <= _CURSOR_SAMPLE_BYTES * 2
        else line[:_CURSOR_SAMPLE_BYTES] + line[-_CURSOR_SAMPLE_BYTES:]
    )
    framed = len(line).to_bytes(8, "big") + sample
    return hashlib.sha256(framed).hexdigest()[:32]


def encode_trace_cursor(
    *,
    event_id: str,
    start: int,
    end: int,
    line: bytes,
) -> str:
    if start < 0 or end <= start or end - start != len(line):
        raise ValueError("Trace cursor offset 与事件行长度不一致")
    raw = json.dumps(
        {"e": event_id, "s": start, "n": end, "h": _cursor_digest(line)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _TRACE_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_trace_cursor(cursor: str) -> _TraceCursorPosition | None:
    if not cursor.startswith(_TRACE_CURSOR_PREFIX):
        return None
    try:
        encoded = cursor.removeprefix(_TRACE_CURSOR_PREFIX)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw)
        position = _TraceCursorPosition(
            event_id=value["e"],
            start=value["s"],
            end=value["n"],
            digest=value["h"],
        )
    except Exception as error:
        raise ValueError("Trace transport cursor 格式无效") from error
    if (
        not position.event_id
        or position.start < 0
        or position.end <= position.start
        or len(position.digest) != 32
    ):
        raise ValueError("Trace transport cursor 字段无效")
    return position


def _offset_from_transport_cursor(file: Path, cursor: str) -> int | None:
    position = _decode_trace_cursor(cursor)
    if position is None:
        return None
    if not file.is_file() or file.stat().st_size < position.end:
        raise ValueError("Trace transport cursor 越过文件末尾")
    size = position.end - position.start
    with file.open("rb") as stream:
        if position.start > 0:
            stream.seek(position.start - 1)
            if stream.read(1) != b"\n":
                raise ValueError("Trace transport cursor 未指向事件行起点")
        stream.seek(position.start)
        if size <= _CURSOR_SAMPLE_BYTES * 2:
            sample = stream.read(size)
        else:
            prefix = stream.read(_CURSOR_SAMPLE_BYTES)
            stream.seek(position.end - _CURSOR_SAMPLE_BYTES)
            sample = prefix + stream.read(_CURSOR_SAMPLE_BYTES)
    framed = size.to_bytes(8, "big") + sample
    if hashlib.sha256(framed).hexdigest()[:32] != position.digest:
        raise ValueError("Trace transport cursor 对应事件已变化")
    return position.end


def events_after_cursor(
    session_id: str,
    events: list[Event],
    after_event_id: str | None,
) -> list[Event]:
    if after_event_id is None:
        return events
    for index, event in enumerate(events):
        if event.event_id == after_event_id:
            return events[index + 1 :]
    raise TraceCursorGoneError(session_id, after_event_id)


def offset_after_event(
    session_id: str,
    file: Path,
    trace_dir: Path,
    after_event_id: str,
) -> int:
    if not file.exists():
        raise TraceCursorGoneError(session_id, after_event_id)
    try:
        transport_offset = _offset_from_transport_cursor(file, after_event_id)
    except ValueError as error:
        raise TraceCursorGoneError(session_id, after_event_id) from error
    if transport_offset is not None:
        return transport_offset
    indexed_offset = TraceTurnIndex(trace_dir).trace_offset_after_event(after_event_id)
    if indexed_offset is not None:
        return indexed_offset
    raise TraceCursorGoneError(session_id, after_event_id)
