from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.schemas.event import Event
from app.schemas.public_v2.trace import TraceEventDTO
from app.schemas.public_v2.turn import TurnAttachmentDTO
from app.services.mapping.trace_event_mapper import TraceEventMapper

_ITEM_EVENT_TYPES = frozenset(
    {
        "job_merged",
        "job_cancelled",
        "job_failed",
        "text_start",
        "text_end",
        "tool_call_start",
        "tool_call_end",
        "error",
        "session_interrupted",
    }
)
_DISPLAY_METADATA_KEYS = frozenset(
    {"goal_continuation", "goal_id", "internal", "message_type", "source"}
)
_INLINE_MEDIA_KEYS = frozenset(
    {"data", "data_url", "image", "image_url", "inline_data", "inline_data_url"}
)


def map_turn_item(
    trace_mapper: TraceEventMapper,
    session_id: str,
    event: Event,
) -> TraceEventDTO | None:
    if event.type not in _ITEM_EVENT_TYPES:
        return None
    raw = strip_inline_media(event.model_dump(mode="python"))
    if event.type == "text_end" and isinstance(raw, dict):
        payload = raw.get("payload")
        if isinstance(payload, dict):
            raw = {**raw, "payload": {**payload, "text": ""}}
    return trace_mapper.map_one(raw, session_id=session_id)


def strip_inline_media(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _INLINE_MEDIA_KEYS:
        return None
    if isinstance(value, str):
        return None if value.startswith("data:") else value
    if isinstance(value, Mapping):
        return {
            str(child_key): strip_inline_media(child, key=str(child_key))
            for child_key, child in value.items()
            if str(child_key).lower() not in _INLINE_MEDIA_KEYS
        }
    if isinstance(value, list):
        return [strip_inline_media(item) for item in value]
    return value


def turn_attachments(values: list[Any]) -> list[TurnAttachmentDTO]:
    attachments: list[TurnAttachmentDTO] = []
    for value in values:
        raw = value.model_dump(mode="python") if hasattr(value, "model_dump") else value
        if not isinstance(raw, Mapping):
            raise TypeError(f"Turn 附件引用必须是 object: {raw!r}")
        file_id = raw.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError(f"Turn 附件引用缺少 file_id: {raw!r}")
        attachments.append(
            TurnAttachmentDTO(
                file_id=file_id,
                name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                content_type=(
                    raw.get("content_type")
                    if isinstance(raw.get("content_type"), str)
                    else None
                ),
            )
        )
    return attachments


def display_metadata(metadata: Mapping[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in _DISPLAY_METADATA_KEYS:
        if key not in metadata or key.lower() in _INLINE_MEDIA_KEYS:
            continue
        value = metadata[key]
        if value is None or isinstance(value, bool | int | float):
            result[key] = value
        elif isinstance(value, str) and not value.startswith("data:"):
            result[key] = value[:512]
    return result


def response_fields(text: str) -> dict[str, object]:
    return {
        "final_response": text,
        "response_preview": text[:1000],
        "preview_truncated": len(text) > 1000,
    }
