from __future__ import annotations

import logging
import os
from pathlib import Path

from app.abstractions.trace_event_sink import TraceAppendReceipt
from app.schemas.event import Event

from .trace_index import TraceTurnIndex
from .trace_index_compaction import is_turn_projected_event

logger = logging.getLogger(__name__)

MESSAGE_TRACE_TYPES = frozenset(
    {
        "job_created",
        "job_started",
        "job_completed",
        "job_cancelled",
        "job_failed",
        "message_created",
        "status_change",
        "text_start",
        "text_end",
        "tool_call_start",
        "tool_call_end",
        "error",
        "session_interrupted",
    }
)


class TraceEventWriter:
    """按 message 派生文件、权威 Trace、轻量索引的顺序提交事件。"""

    def __init__(
        self,
        *,
        trace_file: Path,
        message_file: Path,
        indexed_event_types: frozenset[str],
    ) -> None:
        self._trace_file = trace_file
        self._message_file = message_file
        self._indexed_event_types = indexed_event_types
        self._turn_index = TraceTurnIndex(trace_file.parent)

    def append(self, session_id: str, event: Event) -> TraceAppendReceipt:
        line = event.model_dump_json().encode("utf-8") + b"\n"
        trace_start = self._turn_index.recover_before_append()
        if event.type not in self._indexed_event_types:
            written_end = self._append_bytes(
                self._trace_file,
                line,
                durable=False,
            )
            if written_end != trace_start + len(line):
                raise RuntimeError(
                    "Trace 文件在 append 临界区内发生变化: "
                    f"expected_end={trace_start + len(line)}, actual={written_end}"
                )
            return TraceAppendReceipt(
                event_id=event.event_id,
                trace_end_offset=written_end,
            )

        prepared = self._turn_index.prepare(
            event,
            trace_start=trace_start,
            serialized_size=len(line),
            projects_turn=is_turn_projected_event(event),
        )
        try:
            self._append_bytes(self._message_file, line, durable=True)
            written_end = self._append_bytes(
                self._trace_file,
                line,
                durable=True,
            )
            self._turn_index.commit(prepared)
        except Exception:
            logger.exception(
                "写入 trace 文件失败: session_id=%s event_id=%s",
                session_id,
                event.event_id,
            )
            raise
        return TraceAppendReceipt(
            event_id=event.event_id,
            trace_end_offset=written_end,
            projected_event_offset=prepared.entry.message_end,
        )

    @staticmethod
    def _append_bytes(file: Path, payload: bytes, *, durable: bool) -> int:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
            return stream.tell()
