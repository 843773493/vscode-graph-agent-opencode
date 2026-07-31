from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import RootModel

from app.schemas.event import Event

from .trace_cursor import TraceCursorGoneError

TRACE_PAGE_MAX_BYTES = 512 * 1024
_TRACE_PAGE_CURSOR_PREFIX = "tp1."
_CURSOR_SAMPLE_BYTES = 256


class TracePageBudgetExceededError(RuntimeError):
    """单条 Trace 事件超过诊断页固定读取预算。"""


class _AnyEvent(RootModel[Event]):
    pass


@dataclass(frozen=True, slots=True)
class TraceEventPage:
    events: list[Event]
    next_cursor: str | None
    has_more: bool
    bytes_read: int


@dataclass(frozen=True, slots=True)
class _PageCursorPosition:
    start: int
    end: int
    digest: str


def _line_digest(line: bytes) -> str:
    sample = (
        line
        if len(line) <= _CURSOR_SAMPLE_BYTES * 2
        else line[:_CURSOR_SAMPLE_BYTES] + line[-_CURSOR_SAMPLE_BYTES:]
    )
    framed = len(line).to_bytes(8, "big") + sample
    return hashlib.sha256(framed).hexdigest()[:32]


def _encode_page_cursor(*, start: int, end: int, line: bytes) -> str:
    raw = json.dumps(
        {"s": start, "n": end, "h": _line_digest(line)},
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
            start=value["s"],
            end=value["n"],
            digest=value["h"],
        )
    except Exception as error:
        raise ValueError("Trace page cursor 格式无效") from error
    if (
        position.start < 0
        or position.end <= position.start
        or len(position.digest) != 32
    ):
        raise ValueError("Trace page cursor 字段无效")
    return position


def _validate_page_cursor(file: Path, cursor: str) -> int:
    position = _decode_page_cursor(cursor)
    if not file.is_file() or file.stat().st_size < position.end:
        raise ValueError("Trace page cursor 越过文件末尾")
    size = position.end - position.start
    with file.open("rb") as stream:
        if position.start > 0:
            stream.seek(position.start - 1)
            if stream.read(1) != b"\n":
                raise ValueError("Trace page cursor 未指向事件行起点")
        stream.seek(position.start)
        if size <= _CURSOR_SAMPLE_BYTES * 2:
            sample = stream.read(size)
        else:
            prefix = stream.read(_CURSOR_SAMPLE_BYTES)
            stream.seek(position.end - _CURSOR_SAMPLE_BYTES)
            sample = prefix + stream.read(_CURSOR_SAMPLE_BYTES)
    framed = size.to_bytes(8, "big") + sample
    if hashlib.sha256(framed).hexdigest()[:32] != position.digest:
        raise ValueError("Trace page cursor 对应事件已变化")
    return position.start


def read_trace_event_page(
    *,
    session_id: str,
    file: Path,
    cursor: str | None,
    limit: int,
    max_bytes: int = TRACE_PAGE_MAX_BYTES,
) -> TraceEventPage:
    if limit < 1:
        raise ValueError("Trace page limit 必须大于 0")
    if max_bytes < 1:
        raise ValueError("Trace page max_bytes 必须大于 0")
    if not file.is_file() or file.stat().st_size == 0:
        if cursor is not None:
            raise TraceCursorGoneError(session_id, cursor)
        return TraceEventPage(events=[], next_cursor=None, has_more=False, bytes_read=0)
    try:
        page_end = (
            _validate_page_cursor(file, cursor)
            if cursor is not None
            else file.stat().st_size
        )
    except ValueError as error:
        raise TraceCursorGoneError(session_id, cursor or "") from error
    if page_end == 0:
        return TraceEventPage(events=[], next_cursor=None, has_more=False, bytes_read=0)

    read_start = max(0, page_end - max_bytes)
    with file.open("rb") as stream:
        stream.seek(page_end - 1)
        if stream.read(1) != b"\n":
            raise RuntimeError("Trace 诊断页边界不是完整 JSONL 行")
        stream.seek(read_start)
        payload = stream.read(page_end - read_start)
    bytes_read = len(payload)
    if read_start > 0:
        first_newline = payload.find(b"\n")
        if first_newline < 0:
            raise TracePageBudgetExceededError(
                f"Trace 单事件超过诊断页读取预算: max_bytes={max_bytes}"
            )
        read_start += first_newline + 1
        payload = payload[first_newline + 1 :]

    lines = payload.splitlines(keepends=True)
    if not lines and read_start > 0:
        raise TracePageBudgetExceededError(
            f"Trace 单事件超过诊断页读取预算: max_bytes={max_bytes}"
        )
    offsets: list[tuple[int, int, bytes]] = []
    offset = read_start
    for line in lines:
        line_end = offset + len(line)
        if line.strip():
            offsets.append((offset, line_end, line))
        offset = line_end
    selected = offsets[-limit:]
    events: list[Event] = []
    for line_start, _, line in selected:
        try:
            events.append(_AnyEvent.model_validate_json(line).root)
        except Exception as error:
            raise RuntimeError(
                "Trace 事件协议无效: "
                f"session_id={session_id}, offset={line_start}, line={line[:200]!r}"
            ) from error
    if not selected:
        return TraceEventPage(
            events=[], next_cursor=None, has_more=False, bytes_read=bytes_read
        )
    oldest_start, oldest_end, oldest_line = selected[0]
    has_more = oldest_start > 0
    return TraceEventPage(
        events=events,
        next_cursor=(
            _encode_page_cursor(
                start=oldest_start,
                end=oldest_end,
                line=oldest_line,
            )
            if has_more
            else None
        ),
        has_more=has_more,
        bytes_read=bytes_read,
    )
