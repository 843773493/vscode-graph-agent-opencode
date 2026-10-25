from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path

from pydantic import ValidationError

from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.event import Event

from .trace_cursor import (
    TraceCursorGoneError,
    TraceStreamRecord,
    encode_trace_cursor,
)
from .trace_index_compaction import _AnyEvent


def parse_trace_event_lines(*, session_id: str, lines: Iterable[str]) -> list[Event]:
    """逐行解析 trace，并保留格式损坏优先于事件协议错误的归因顺序。"""
    events: list[Event] = []
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
            events.append(_AnyEvent.model_validate(raw_event).root)
        except ValidationError as error:
            first_schema_error = (raw_event, error)
            events.clear()
    if first_schema_error is not None:
        raw_event, error = first_schema_error
        raise RuntimeError(
            f"Trace 事件协议无效: session_id={session_id} event={raw_event!r}"
        ) from error
    return events


def _read_trace_suffix(
    *,
    session_id: str,
    file: Path,
    offset: int,
) -> tuple[int, list[tuple[dict[str, object], bytes, int, int]]]:
    with file.open("rb") as stream:
        stream.seek(offset)
        raw_records: list[tuple[dict[str, object], bytes, int, int]] = []
        while line := stream.readline():
            line_start = offset
            offset = stream.tell()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_event = json.loads(stripped.decode("utf-8"))
            except Exception as error:
                raise RuntimeError(
                    "Trace 流遇到损坏行: "
                    f"session_id={session_id} "
                    f"line={stripped[:200]!r}"
                ) from error
            if not isinstance(raw_event, dict):
                raise TypeError(
                    f"Trace 流事件必须是 JSON object: session_id={session_id}"
                )
            raw_records.append((raw_event, line, line_start, offset))

    return offset, raw_records


async def stream_trace_records(
    *,
    session_id: str,
    file: Path,
    initial_offset: int,
    requested_cursor: str | None,
    condition: asyncio.Condition,
) -> AsyncGenerator[TraceStreamRecord, None]:
    offset = initial_offset
    while True:
        if file.exists():
            file_size = file.stat().st_size
            if file_size < offset:
                raise TraceCursorGoneError(
                    session_id,
                    requested_cursor or "<stream-offset>",
                )
            if file_size > offset:
                offset, raw_records = await run_sync_shielded(
                    _read_trace_suffix,
                    session_id=session_id,
                    file=file,
                    offset=offset,
                )
                for raw_event, line, line_start, line_end in raw_records:
                    try:
                        event = _AnyEvent.model_validate(raw_event).root
                    except Exception as error:
                        raise RuntimeError(
                            "Trace 流事件协议无效: "
                            f"session_id={session_id} event={raw_event!r}"
                        ) from error
                    yield TraceStreamRecord(
                        event=event,
                        cursor=encode_trace_cursor(
                            event_id=event.event_id,
                            start=line_start,
                            end=line_end,
                            line=line,
                        ),
                    )

        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout=1.0)
            except TimeoutError:
                pass
