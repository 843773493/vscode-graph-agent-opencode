from __future__ import annotations

from pathlib import Path

from pydantic import RootModel

from app.abstractions.turn_history import TurnBootstrapBatch, TurnIndexedEvent
from app.schemas.event import Event

from .trace_index_compaction import is_turn_projected_event


class _AnyEvent(RootModel[Event]):
    pass


def read_legacy_bootstrap(
    message_path: Path,
    *,
    max_events: int,
    max_bytes: int,
) -> TurnBootstrapBatch:
    if not message_path.exists() or message_path.stat().st_size == 0:
        return TurnBootstrapBatch(index_available=False)
    if message_path.stat().st_size > max_bytes:
        return TurnBootstrapBatch(
            has_older_events=True,
            index_available=False,
        )
    raw_lines = message_path.read_bytes().splitlines(keepends=True)
    indexed_events: list[tuple[Event, int]] = []
    offset = 0
    for line in raw_lines:
        offset += len(line)
        if line.strip():
            indexed_events.append((_AnyEvent.model_validate_json(line).root, offset))
    indexed_events = indexed_events[-max_events:]
    events = [event for event, _ in indexed_events]
    latest_index = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if events[index].type == "job_created"
        ),
        None,
    )
    if latest_index is None:
        return TurnBootstrapBatch(
            event_cursor=events[-1].event_id if events else None,
            has_older_events=True,
            index_available=False,
        )
    selected = indexed_events[latest_index:]
    latest_projected = next(
        (
            indexed_event
            for indexed_event in reversed(indexed_events)
            if is_turn_projected_event(indexed_event[0])
        ),
        None,
    )
    return TurnBootstrapBatch(
        events=[
            TurnIndexedEvent(event=event, source_offset=source_offset)
            for event, source_offset in selected
        ],
        event_cursor=(latest_projected[0].event_id if latest_projected else None),
        event_offset=(latest_projected[1] if latest_projected else None),
        has_older_events=latest_index > 0,
        index_available=False,
    )
