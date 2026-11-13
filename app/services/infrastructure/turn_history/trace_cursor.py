from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from app.schemas.event import Event


class TraceCursorGoneError(RuntimeError):
    """请求的事件游标已不在当前 Trace 中。"""

    def __init__(self, session_id: str, cursor: str) -> None:
        self.session_id = session_id
        self.cursor = cursor
        super().__init__(
            f"Trace 事件游标不存在: session_id={session_id}, cursor={cursor}"
        )


def bind_trace_cursor(
    cursor: str | None, *, session_id: str, thread_id: str | None, kind: str
) -> str | None:
    """公开游标绑定查询身份；内部逻辑偏移复用已提交事件校验。"""
    if cursor is None:
        return None
    payload = json.dumps(
        [session_id, thread_id, kind, cursor], separators=(",", ":")
    ).encode()
    return "ts2." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def unbind_trace_cursor(
    cursor: str | None, *, session_id: str, thread_id: str | None, kind: str
) -> str | None:
    if cursor is None:
        return None
    try:
        if not cursor.startswith("ts2."):
            raise ValueError("Trace scope cursor 前缀无效")
        encoded = cursor[4:]
        value = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if (
            not isinstance(value, list)
            or len(value) != 4
            or value[:3] != [session_id, thread_id, kind]
            or not isinstance(value[3], str)
            or not value[3]
        ):
            raise ValueError("Trace scope cursor 的 Session/Thread/用途不匹配")
        return value[3]
    except (ValueError, TypeError, UnicodeError) as error:
        raise TraceCursorGoneError(session_id, cursor) from error


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
TRACE_EVENT_LINE_SAMPLE_BYTES = 256


def event_line_sample(line: bytes) -> bytes:
    """取事件行首尾字节样本；短行直接整行参与摘要。"""
    limit = TRACE_EVENT_LINE_SAMPLE_BYTES
    if len(line) <= limit * 2:
        return line
    return line[:limit] + line[-limit:]


def event_line_digest(size: int, sample: bytes) -> str:
    """事件行摘要：长度前缀加首尾样本，传输游标与诊断页游标共用此唯一实现。"""
    return hashlib.sha256(size.to_bytes(8, "big") + sample).hexdigest()[:32]


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
        {
            "e": event_id,
            "s": start,
            "n": end,
            "h": event_line_digest(len(line), event_line_sample(line)),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _TRACE_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip(
        "="
    )


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


def _read_and_verify_line(
    connection: sqlite3.Connection | None,
    *,
    start: int,
    end: int,
    digest: str,
    label: str,
) -> None:
    row = (
        connection.execute(
            "SELECT end, substr(CAST(raw AS BLOB),1,?), substr(CAST(raw AS BLOB),-?) FROM trace_events WHERE start=?",
            (TRACE_EVENT_LINE_SAMPLE_BYTES, TRACE_EVENT_LINE_SAMPLE_BYTES, start),
        ).fetchone()
        if connection is not None
        else None
    )
    if row is None or row[0] != end:
        raise ValueError(f"{label} 未指向已提交事件")
    size = end - start
    # 短行首尾样本重叠，仍与编码时的整行摘要完全一致。
    sample = (
        (row[1] + row[2][-min(TRACE_EVENT_LINE_SAMPLE_BYTES, size - len(row[1])) :])
        if size > len(row[1])
        else row[1]
    )
    if event_line_digest(size, sample) != digest:
        raise ValueError(f"{label} 对应事件已变化")


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
    connection: sqlite3.Connection | None,
    after_event_id: str,
) -> int:
    try:
        position = _decode_trace_cursor(after_event_id)
        if position is not None:
            _read_and_verify_line(
                connection,
                start=position.start,
                end=position.end,
                digest=position.digest,
                label="Trace transport cursor",
            )
            row = connection.execute(
                "SELECT event_id FROM trace_events WHERE start=?", (position.start,)
            ).fetchone()
            if row[0] != position.event_id:
                raise ValueError("Trace transport cursor 的事件身份不一致")
            return position.end
    except ValueError as error:
        raise TraceCursorGoneError(session_id, after_event_id) from error
    row = (
        connection.execute(
            "SELECT end,is_message FROM trace_events WHERE event_id=? ORDER BY start LIMIT 1",
            (after_event_id,),
        ).fetchone()
        if connection is not None
        else None
    )
    if row is not None and row[1]:
        return row[0]
    raise TraceCursorGoneError(session_id, after_event_id)
