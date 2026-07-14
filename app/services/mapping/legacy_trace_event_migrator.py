from __future__ import annotations

from copy import deepcopy
from typing import Any


PART_EVENT_TYPES = {
    "text_start",
    "text_delta",
    "text_end",
    "tool_call_start",
    "tool_call_end",
}


def _part_kind(payload: dict[str, Any]) -> str:
    return "reasoning" if payload.get("kind") == "reasoning" else "markdown"


def _synthetic_event(
    source: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    part_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "part_id": part_id,
        "job_id": source.get("job_id") or "legacy_job",
        "step_id": source.get("step_id"),
        "agent_id": source.get("agent_id"),
        "timestamp": source.get("timestamp"),
        "type": event_type,
        "payload": payload,
    }


def migrate_legacy_trace_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把旧 trace 明确升级为带稳定 part_id 的当前事件协议。"""
    migrated: list[dict[str, Any]] = []
    pending_text_start: dict[str, Any] | None = None
    current_text_part_id: str | None = None
    current_text_kind: str | None = None
    current_text_chunks: list[str] = []
    pending_tools: dict[str, list[str]] = {}

    def close_text_part(boundary: dict[str, Any]) -> None:
        nonlocal current_text_part_id, current_text_kind, current_text_chunks
        if current_text_part_id is None or current_text_kind is None:
            return
        migrated.append(
            _synthetic_event(
                boundary,
                event_id=f"evt_migrated_end_{current_text_part_id}",
                event_type="text_end",
                part_id=current_text_part_id,
                payload={"kind": current_text_kind, "text": "".join(current_text_chunks)},
            )
        )
        current_text_part_id = None
        current_text_kind = None
        current_text_chunks = []

    def start_text_part(source: dict[str, Any], kind: str) -> None:
        nonlocal pending_text_start, current_text_part_id, current_text_kind
        start_source = pending_text_start or source
        start_event_id = str(start_source.get("event_id") or source.get("event_id") or "legacy")
        current_text_part_id = f"part_legacy_{start_event_id}"
        current_text_kind = kind
        start_event = deepcopy(start_source)
        start_event["part_id"] = current_text_part_id
        start_event["type"] = "text_start"
        start_event["payload"] = {"kind": kind}
        migrated.append(start_event)
        pending_text_start = None

    for original in events:
        event = deepcopy(original)
        event_type = event.get("type")
        payload_value = event.get("payload")
        payload = payload_value if isinstance(payload_value, dict) else {}

        if event_type not in PART_EVENT_TYPES:
            if event_type in {
                "tool_call_start",
                "agent_start",
                "agent_end",
                "llm_request",
                "error",
                "job_failed",
                "job_cancelled",
                "session_interrupted",
            }:
                close_text_part(event)
            migrated.append(event)
            continue

        if event.get("part_id"):
            event["payload"] = payload
            migrated.append(event)
            if event_type == "text_start":
                current_text_part_id = str(event["part_id"])
                current_text_kind = _part_kind(payload)
                current_text_chunks = []
            elif event_type == "text_delta":
                text = payload.get("text")
                if isinstance(text, str):
                    current_text_chunks.append(text)
            elif event_type == "text_end":
                current_text_part_id = None
                current_text_kind = None
                current_text_chunks = []
            continue

        if event_type == "text_start":
            pending_text_start = event
            continue

        if event_type == "text_delta":
            kind = _part_kind(payload)
            if current_text_kind != kind:
                close_text_part(event)
            if current_text_part_id is None:
                start_text_part(event, kind)
            else:
                pending_text_start = None
            text = payload.get("text")
            normalized_text = text if isinstance(text, str) else ""
            current_text_chunks.append(normalized_text)
            event["part_id"] = current_text_part_id
            event["payload"] = {"kind": kind, "text": normalized_text}
            migrated.append(event)
            continue

        if event_type == "text_end":
            final_text = payload.get("text")
            normalized_text = final_text if isinstance(final_text, str) else ""
            if current_text_kind != "markdown":
                close_text_part(event)
            else:
                pending_text_start = None
            if current_text_part_id is None:
                start_text_part(event, "markdown")
            event["part_id"] = current_text_part_id
            event["payload"] = {"kind": "markdown", "text": normalized_text}
            migrated.append(event)
            current_text_part_id = None
            current_text_kind = None
            current_text_chunks = []
            continue

        close_text_part(event)
        pending_text_start = None
        tool_name_value = payload.get("tool_name")
        tool_name = tool_name_value if isinstance(tool_name_value, str) else "unknown_tool"
        old_run_id = payload.pop("tool_call_run_id", None)
        if event_type == "tool_call_start":
            part_id = (
                old_run_id
                if isinstance(old_run_id, str) and old_run_id
                else f"part_legacy_{event.get('event_id') or tool_name}"
            )
            pending_tools.setdefault(tool_name, []).append(part_id)
        else:
            candidates = pending_tools.get(tool_name, [])
            part_id = (
                old_run_id
                if isinstance(old_run_id, str) and old_run_id
                else candidates.pop(0) if candidates else ""
            )
            if not part_id:
                part_id = f"part_legacy_{event.get('event_id') or tool_name}"
                migrated.append(
                    _synthetic_event(
                        event,
                        event_id=f"evt_migrated_start_{event.get('event_id') or tool_name}",
                        event_type="tool_call_start",
                        part_id=part_id,
                        payload={"tool_name": tool_name, "args": {}},
                    )
                )
        event["part_id"] = part_id
        event["payload"] = payload
        migrated.append(event)

    return _merge_adjacent_legacy_text_parts(migrated)


def _merge_adjacent_legacy_text_parts(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并旧生产器在同一连续文本中反复结束并重启的碎片 part。"""
    merged: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    accumulated_text: dict[str, str] = {}

    for original in events:
        event = deepcopy(original)
        part_id_value = event.get("part_id")
        if isinstance(part_id_value, str) and part_id_value in aliases:
            event["part_id"] = aliases[part_id_value]
        event_type = event.get("type")
        payload_value = event.get("payload")
        payload = payload_value if isinstance(payload_value, dict) else {}
        part_id = event.get("part_id")

        if (
            event_type == "text_start"
            and isinstance(part_id, str)
            and part_id.startswith("part_legacy_")
            and merged
            and merged[-1].get("type") == "text_end"
        ):
            previous_end = merged[-1]
            previous_part = previous_end.get("part_id")
            previous_payload = previous_end.get("payload")
            previous_kind = (
                previous_payload.get("kind")
                if isinstance(previous_payload, dict)
                else None
            )
            previous_text = (
                previous_payload.get("text")
                if isinstance(previous_payload, dict)
                else None
            )
            if (
                isinstance(previous_part, str)
                and previous_part.startswith("part_legacy_")
                and previous_kind == payload.get("kind")
                and previous_text == accumulated_text.get(previous_part, "")
            ):
                merged.pop()
                aliases[part_id] = previous_part
                continue

        if event_type == "text_delta" and isinstance(part_id, str):
            text = payload.get("text")
            if isinstance(text, str):
                accumulated_text[part_id] = accumulated_text.get(part_id, "") + text
        merged.append(event)

    return merged
