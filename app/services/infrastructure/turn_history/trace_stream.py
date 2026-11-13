from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable, Iterator

from pydantic import ValidationError

from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.event import Event

from .trace_cursor import (
    TraceCursorGoneError,
    TraceStreamRecord,
    encode_trace_cursor,
    offset_after_event,
)
from .trace_database import TraceDatabase
from .trace_page import _AnyEvent, parse_trace_record

_TRACE_STREAM_BATCH_BYTES = 512 * 1024


def iter_trace_event_lines(*, session_id: str, lines: Iterable[str]) -> Iterator[Event]:
    """逐行解析 trace，并保留格式损坏优先于事件协议错误的归因顺序。"""
    first_schema_error: tuple[dict[str, object], ValidationError] | None = None
    for source_line in lines:
        line = source_line.strip()
        if not line:
            continue
        try:
            raw_event = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Trace 行无法解析: session_id={session_id} line={line[:200]!r}"
            ) from error
        if not isinstance(raw_event, dict):
            raise TypeError(
                f"Trace 行必须是 JSON object: session_id={session_id} line={line[:200]!r}"
            )
        if first_schema_error is not None:
            continue
        try:
            event = _AnyEvent.model_validate(raw_event).root
        except ValidationError as error:
            first_schema_error = (raw_event, error)
        else:
            yield event
    if first_schema_error is not None:
        raw_event, error = first_schema_error
        raise RuntimeError(
            f"Trace 事件协议无效: session_id={session_id} event={raw_event!r}"
        ) from error


def _read_trace_batch(
    database: TraceDatabase, session_id: str, offset: int
) -> list[tuple[int, int, bytes]]:
    with database.connection() as connection:
        if connection is None:
            if offset:
                raise TraceCursorGoneError(session_id, "<stream-offset>")
            return []
        if (
            offset
            and connection.execute(
                "SELECT 1 FROM trace_events WHERE end=?", (offset,)
            ).fetchone()
            is None
        ):
            raise TraceCursorGoneError(session_id, "<stream-offset>")
        records: list[tuple[int, int, bytes]] = []
        size = 0
        for row in connection.execute(
            "SELECT start,end,raw FROM trace_events WHERE start>=? ORDER BY start LIMIT 128",
            (offset,),
        ):
            raw = row[2].encode("utf-8")
            records.append((row[0], row[1], raw))
            size += len(raw)
            if size >= _TRACE_STREAM_BATCH_BYTES:
                break
        return records


def _initial_offset(
    database: TraceDatabase, session_id: str, cursor: str | None
) -> int:
    if cursor is None:
        return 0
    with database.connection() as connection:
        return offset_after_event(session_id, connection, cursor)


async def stream_trace_records(
    *,
    session_id: str,
    database: TraceDatabase,
    requested_cursor: str | None,
    condition: asyncio.Condition,
) -> AsyncGenerator[TraceStreamRecord, None]:
    offset = await run_sync_shielded(
        _initial_offset, database, session_id, requested_cursor
    )
    while True:
        # 空快照检查与注册等待共享 Condition，避免提交通知落入两者之间而多等一秒。
        async with condition:
            records = await run_sync_shielded(
                _read_trace_batch, database, session_id, offset
            )
            if not records:
                try:
                    await asyncio.wait_for(condition.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
        for start, end, raw in records:
            record = parse_trace_record(session_id, start, end, raw)
            yield TraceStreamRecord(
                event=record.event,
                cursor=encode_trace_cursor(
                    event_id=record.event.event_id, start=start, end=end, line=raw
                ),
            )
            offset = end
