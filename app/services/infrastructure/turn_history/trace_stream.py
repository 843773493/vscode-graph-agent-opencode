from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path

from pydantic import RootModel

from app.schemas.event import Event

from .trace_cursor import (
    TraceCursorGoneError,
    TraceStreamRecord,
    encode_trace_cursor,
)


class _AnyEvent(RootModel[Event]):
    pass


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
            if file.stat().st_size < offset:
                raise TraceCursorGoneError(
                    session_id,
                    requested_cursor or "<stream-offset>",
                )
            with file.open("rb") as stream:
                stream.seek(offset)
                records: list[tuple[dict[str, object], bytes, int, int]] = []
                while line := stream.readline():
                    start = offset
                    offset = stream.tell()
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw_event = json.loads(stripped.decode("utf-8"))
                    except Exception as exc:
                        raise RuntimeError(
                            f"Trace 流遇到损坏行: session_id={session_id} "
                            f"line={stripped[:200]!r}"
                        ) from exc
                    if not isinstance(raw_event, dict):
                        raise TypeError(
                            f"Trace 流事件必须是 JSON object: session_id={session_id}"
                        )
                    records.append((raw_event, line, start, offset))

                for raw_event, line, start, end in records:
                    try:
                        event = _AnyEvent.model_validate(raw_event).root
                    except Exception as exc:
                        raise RuntimeError(
                            "Trace 流事件协议无效: "
                            f"session_id={session_id} event={raw_event!r}"
                        ) from exc
                    yield TraceStreamRecord(
                        event=event,
                        cursor=encode_trace_cursor(
                            event_id=event.event_id,
                            start=start,
                            end=end,
                            line=line,
                        ),
                    )

        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout=1.0)
            except TimeoutError:
                pass
