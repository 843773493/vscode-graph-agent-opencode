from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable, Iterator

from pydantic import ValidationError

from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.event import Event
from app.services.infrastructure.events.event_channel_service import (
    EventChannel,
    EventChannelService,
    EventChannelSpec,
)

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
    database: TraceDatabase, session_id: str, offset: int,
    thread_id: str | None = None, before: int | None = None,
) -> tuple[list[tuple[int, int, bytes, str | None]], int]:
    with database.connection() as connection:
        if connection is None:
            if offset:
                raise TraceCursorGoneError(session_id, "<stream-offset>")
            return [], 0
        if offset and connection.execute(
            "SELECT 1 FROM trace_events WHERE end=?", (offset,),
        ).fetchone() is None:
            raise TraceCursorGoneError(session_id, "<stream-offset>")
        tail = connection.execute(
            "SELECT end FROM trace_events ORDER BY start DESC LIMIT 1",
        ).fetchone()
        boundary = min(before, tail[0]) if before is not None and tail else (tail[0] if tail else 0)
        scope = " AND thread_id=?" if thread_id is not None else ""
        parameters = (offset, boundary, thread_id) if thread_id is not None else (offset, boundary)
        records: list[tuple[int, int, bytes, str | None]] = []
        size = 0
        for row in connection.execute(
            f"SELECT start,end,raw,thread_id FROM trace_events WHERE start>=? AND start<?{scope} ORDER BY start LIMIT 128",
            parameters,
        ):
            raw = row[2].encode("utf-8")
            records.append((row[0], row[1], raw, row[3]))
            size += len(raw)
            if size >= _TRACE_STREAM_BATCH_BYTES:
                break
        return records, records[-1][1] if records else boundary


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
    event_service: EventChannelService,
    channel_spec: EventChannelSpec,
    thread_id: str | None,
) -> AsyncGenerator[TraceStreamRecord, None]:
    offset = await run_sync_shielded(
        _initial_offset, database, session_id, requested_cursor,
    )
    channel: EventChannel[TraceStreamRecord] | None = None
    subscription = None
    pending: TraceStreamRecord | None = None
    try:
        while True:
            if subscription is not None:
                subscription.check_overflow()
            # 历史、空闲时的跨实例发现及实时序列中的缺口，统一走同一有界读取。
            records, watermark = await run_sync_shielded(
                _read_trace_batch, database, session_id, offset, thread_id,
                pending.start if pending is not None else None,
            )
            for start, end, raw, owner in records:
                if subscription is not None:
                    subscription.check_overflow()
                record = parse_trace_record(session_id, start, end, raw)
                yield TraceStreamRecord(
                    event=record.event,
                    cursor=encode_trace_cursor(
                        event_id=record.event.event_id, start=start, end=end, line=raw,
                    ),
                    start=start, end=end, thread_id=owner,
                )
                offset = end
            offset = watermark
            if records:
                continue
            if subscription is None:
                # 先补完大段历史，再订阅并重查一次，关闭查询与订阅之间的提交窗口。
                channel = event_service.ensure_channel(channel_spec)
                subscription = channel.subscribe(label="session-trace")
                continue
            while True:
                subscription.check_overflow()
                if pending is not None and pending.end > offset:
                    yield pending
                    offset = pending.end
                pending = None
                try:
                    delivery = await asyncio.wait_for(subscription.next(), timeout=1.0)
                except TimeoutError:
                    break
                record = delivery.event
                if record.end <= offset:
                    continue
                pending = record
                if record.start > offset:
                    # 其它 Thread 或其它实例可能在中间提交；先按索引补齐，避免乱序和跳过事件。
                    break
    finally:
        if channel is not None and subscription is not None:
            channel.unsubscribe(subscription)
            event_service.remove_channel_if_unused(channel_spec.name)
