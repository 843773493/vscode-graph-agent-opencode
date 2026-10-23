from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path

from app.core.shielded_cleanup import run_sync_shielded

from .trace_cursor import (
    TraceCursorGoneError,
    TraceStreamRecord,
    encode_trace_cursor,
)
from .trace_index_compaction import _AnyEvent


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
