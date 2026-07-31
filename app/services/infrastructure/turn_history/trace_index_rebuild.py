from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO

from pydantic import RootModel

from app.schemas.event import Event

from .trace_index_compaction import (
    MAX_COMPACT_LINE_BYTES,
    compact_event,
    is_turn_projected_event,
)
from .trace_index_models import TraceTurnIndexEntry, TraceTurnIndexManifest


class _AnyEvent(RootModel[Event]):
    pass


def _read_complete_event(stream: BinaryIO, *, source: str) -> tuple[Event, int, int] | None:
    start = stream.tell()
    line = stream.readline()
    if not line:
        return None
    if not line.endswith(b"\n"):
        raise RuntimeError(f"{source} 尾行不完整，无法重建 Turn index: offset={start}")
    return _AnyEvent.model_validate_json(line).root, start, stream.tell()


def _find_trace_event(
    trace_stream: BinaryIO,
    *,
    expected: Event,
) -> tuple[Event, int, int]:
    while found := _read_complete_event(trace_stream, source="events.jsonl"):
        event, trace_start, trace_end = found
        if event.event_id != expected.event_id:
            continue
        if event.type != expected.type:
            raise RuntimeError(
                "events/messages 语义事件类型不一致: "
                f"event_id={event.event_id}, trace={event.type}, message={expected.type}"
            )
        if event.model_dump(mode="json") != expected.model_dump(mode="json"):
            raise RuntimeError(
                "events/messages 语义事件内容不一致: "
                f"event_id={event.event_id}, type={event.type}"
            )
        return event, trace_start, trace_end
    raise RuntimeError(
        "messages.jsonl 事件在权威 Trace 中不存在: "
        f"event_id={expected.event_id}, type={expected.type}"
    )


def _write_entry(
    stream: BinaryIO,
    *,
    event: Event,
    trace_start: int,
    trace_end: int,
    message_start: int,
    message_end: int,
) -> tuple[TraceTurnIndexEntry, int, int]:
    index_start = stream.tell()
    full_payload = event.model_dump(mode="json")
    compact_payload = compact_event(event)
    entry = TraceTurnIndexEntry(
        event_id=event.event_id,
        event_type=event.type,
        job_id=event.job_id,
        projects_turn=is_turn_projected_event(event),
        trace_start=trace_start,
        trace_end=trace_end,
        message_start=message_start,
        message_end=message_end,
        source_compacted=compact_payload != full_payload,
        compact_event=compact_payload,
    )
    line = entry.model_dump_json().encode("utf-8") + b"\n"
    if len(line) > MAX_COMPACT_LINE_BYTES:
        raise RuntimeError(
            "重建 Trace Turn index 事件超过固定上限: "
            f"event_id={event.event_id}, bytes={len(line)}"
        )
    stream.write(line)
    return entry, index_start, stream.tell()


def _record_manifest_entry(
    manifest: TraceTurnIndexManifest,
    *,
    entry: TraceTurnIndexEntry,
    index_start: int,
    index_end: int,
) -> None:
    manifest.committed_index_offset = index_end
    manifest.committed_trace_offset = entry.trace_end
    manifest.committed_message_offset = entry.message_end
    if entry.projects_turn:
        manifest.event_cursor = entry.event_id
        manifest.projected_message_offset = entry.message_end
        manifest.projected_trace_offset = entry.trace_end
    if entry.event_type == "job_created":
        manifest.latest_job_index_offset = index_start
        manifest.latest_job_id = entry.job_id


def _build_temp_index(
    *,
    trace_path: Path,
    message_path: Path,
    temp_index: Path,
    indexed_event_types: frozenset[str],
) -> TraceTurnIndexManifest:
    manifest = TraceTurnIndexManifest()
    trace_source = trace_path if trace_path.exists() else Path(os.devnull)
    message_source = message_path if message_path.exists() else Path(os.devnull)
    with (
        trace_source.open("rb") as trace_stream,
        message_source.open("rb") as message_stream,
        temp_index.open("wb") as index_stream,
    ):
        while message_record := _read_complete_event(
            message_stream,
            source="messages.jsonl",
        ):
            message_event, message_start, message_end = message_record
            if message_event.type not in indexed_event_types:
                raise RuntimeError(
                    "messages.jsonl 含不支持的语义事件: "
                    f"event_id={message_event.event_id}, type={message_event.type}"
                )
            event, trace_start, trace_end = _find_trace_event(
                trace_stream,
                expected=message_event,
            )
            entry, index_start, index_end = _write_entry(
                index_stream,
                event=event,
                trace_start=trace_start,
                trace_end=trace_end,
                message_start=message_start,
                message_end=message_end,
            )
            _record_manifest_entry(
                manifest,
                entry=entry,
                index_start=index_start,
                index_end=index_end,
            )
        index_stream.flush()
        os.fsync(index_stream.fileno())
    return manifest


def rebuild_trace_turn_index(
    *,
    trace_path: Path,
    message_path: Path,
    trace_dir: Path,
    indexed_event_types: frozenset[str],
) -> TraceTurnIndexManifest:
    """以旧 messages 顺序匹配权威 Trace，并原子发布轻量索引。"""

    trace_dir.mkdir(parents=True, exist_ok=True)
    index_path = trace_dir / "turn-events.index.jsonl"
    manifest_path = trace_dir / "turn-events.index.json"
    suffix = f"{os.getpid()}.{threading.get_ident()}.tmp"
    temp_index = index_path.with_name(f".{index_path.name}.{suffix}")
    temp_manifest = manifest_path.with_name(f".{manifest_path.name}.{suffix}")
    try:
        manifest = _build_temp_index(
            trace_path=trace_path,
            message_path=message_path,
            temp_index=temp_index,
            indexed_event_types=indexed_event_types,
        )
        with temp_manifest.open("wb") as stream:
            stream.write(manifest.model_dump_json().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_index, index_path)
        os.replace(temp_manifest, manifest_path)
        directory_fd = os.open(trace_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return manifest
    finally:
        temp_index.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)
