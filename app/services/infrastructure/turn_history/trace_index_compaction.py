from __future__ import annotations

from typing import Any

from pydantic import RootModel

from app.schemas.event import Event


class _AnyEvent(RootModel[Event]):
    pass


INLINE_MEDIA_KEYS = frozenset(
    {"data", "data_url", "image", "image_url", "inline_data", "inline_data_url"}
)
COMPACT_STRING_CHARS = 2048
COMPACT_TOTAL_CHARS = 24 * 1024
MAX_COMPACT_LINE_BYTES = 64 * 1024
TURN_PROJECTED_EVENT_TYPES = frozenset(
    {
        "job_created",
        "job_started",
        "job_completed",
        "job_cancelled",
        "job_failed",
        "status_change",
        "text_start",
        "text_end",
        "tool_call_start",
        "tool_call_end",
        "error",
        "session_interrupted",
    }
)


def is_turn_projected_event(event: Event) -> bool:
    return event.type in TURN_PROJECTED_EVENT_TYPES or (
        event.type == "message_created" and event.payload.role == "user"
    )


def compact_event(event: Event) -> dict[str, object]:
    remaining = [COMPACT_TOTAL_CHARS]
    compact = _compact_value(event.model_dump(mode="json"), remaining=remaining)
    if not isinstance(compact, dict):
        raise TypeError("Trace compact event 必须是 object")
    _AnyEvent.model_validate(compact)
    return compact


def _compact_value(
    value: Any,
    *,
    remaining: list[int],
    key: str | None = None,
) -> object:
    if key is not None and key.lower() in INLINE_MEDIA_KEYS:
        return None
    if isinstance(value, str):
        length = min(len(value), COMPACT_STRING_CHARS, max(0, remaining[0]))
        remaining[0] -= length
        return value[:length]
    if isinstance(value, list):
        return [_compact_value(item, remaining=remaining) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(child_key): _compact_value(
                child,
                remaining=remaining,
                key=str(child_key),
            )
            for child_key, child in list(value.items())[:64]
        }
    return value
