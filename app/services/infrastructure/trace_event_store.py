from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

from pydantic import RootModel

from app.abstractions.trace_event_sink import TraceAppendReceipt
from app.abstractions.turn_history import (
    TurnBootstrapBatch,
    TurnMigrationSnapshot,
    TurnRecoveryBatch,
)
from app.core.path_utils import get_session_path_resolver
from app.schemas.event import Event
from app.services.infrastructure.turn_history.trace_cursor import (
    TraceCursorGoneError,
    encode_trace_cursor,
    events_after_cursor,
    offset_after_event,
)
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex
from app.services.infrastructure.turn_history.trace_index_rebuild import (
    rebuild_trace_turn_index,
)
from app.services.infrastructure.turn_history.trace_page import (
    TRACE_PAGE_MAX_BYTES,
    TraceEventPage,
    read_trace_event_page,
)
from app.services.infrastructure.turn_history.trace_stream import stream_trace_records
from app.services.infrastructure.turn_history.trace_writer import (
    MESSAGE_TRACE_TYPES,
    TraceEventWriter,
)

__all__ = ["TraceCursorGoneError", "TraceEventStore"]


class _AnyEvent(RootModel[Event]):
    pass


class TraceEventStore:
    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._path_resolver = get_session_path_resolver(sessions_dir)
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._append_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._file_locks: defaultdict[str, threading.RLock] = defaultdict(
            threading.RLock
        )

    def _trace_file(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id)
            / "logs"
            / "traces"
            / "events.jsonl"
        )

    def list_session_ids(self) -> list[str]:
        return sorted(
            node.node_id
            for node in self._path_resolver.list_nodes()
            if node.kind == "session"
        )

    def _message_trace_file(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id)
            / "logs"
            / "traces"
            / "messages.jsonl"
        )

    async def _notify(self, session_id: str) -> None:
        condition = self._conditions.get(session_id)
        if condition is None:
            return
        async with condition:
            condition.notify_all()

    def _append_event_files(
        self,
        session_id: str,
        event: Event,
    ) -> TraceAppendReceipt:
        with self._file_locks[session_id]:
            return TraceEventWriter(
                trace_file=self._trace_file(session_id),
                message_file=self._message_trace_file(session_id),
                indexed_event_types=MESSAGE_TRACE_TYPES,
            ).append(session_id, event)

    async def append(self, session_id: str, event: Event) -> TraceAppendReceipt:
        async with self._append_locks[session_id]:
            receipt = await asyncio.to_thread(
                self._append_event_files,
                session_id,
                event,
            )
        await self._notify(session_id)
        return receipt

    def read_turn_bootstrap_batch(
        self,
        session_id: str,
        *,
        max_events: int,
        max_bytes: int,
    ) -> TurnBootstrapBatch:
        with self._file_locks[session_id]:
            return TraceTurnIndex(self._trace_file(session_id).parent).bootstrap_batch(
                max_events=max_events,
                max_bytes=max_bytes,
            )

    def ensure_turn_index(self, session_id: str) -> None:
        with self._file_locks[session_id]:
            index = TraceTurnIndex(self._trace_file(session_id).parent)
            snapshot = index.snapshot()
            if snapshot is not None and not snapshot.has_unindexed_prefix:
                return
            rebuild_trace_turn_index(
                trace_path=self._trace_file(session_id),
                message_path=self._message_trace_file(session_id),
                trace_dir=self._trace_file(session_id).parent,
                indexed_event_types=MESSAGE_TRACE_TYPES,
            )

    def read_turn_recovery_batch(
        self,
        session_id: str,
        *,
        after_event_id: str | None,
        max_events: int,
        max_bytes: int,
    ) -> TurnRecoveryBatch:
        with self._file_locks[session_id]:
            return TraceTurnIndex(self._trace_file(session_id).parent).recovery_batch(
                after_event_id=after_event_id,
                max_events=max_events,
                max_bytes=max_bytes,
            )

    def read_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
        tail_limit: int | None = None,
    ) -> list[Event]:
        events = self._read_file_events(
            session_id,
            self._trace_file(session_id),
            tail_limit=tail_limit if after_event_id is None else None,
        )
        return events_after_cursor(session_id, events, after_event_id)

    def read_terminal_job_statuses(
        self,
        session_id: str,
        job_ids: set[str],
        *,
        tail_limit: int = 4096,
    ) -> dict[str, str]:
        """从持久化 Trace 尾部恢复指定 Job 的终态。

        只在 rollout 中仍为 running 的 Turn 上调用，用于覆盖服务进程重启前
        尚未来得及写入 turns.status 的失败或取消任务；正常历史读取不会扫描
        Trace，也不会改变新的 rollout/SQLite 快路径。
        """
        if not job_ids:
            return {}
        terminal_types = {
            "job_completed": "completed",
            "job_failed": "failed",
            "job_cancelled": "cancelled",
        }
        statuses: dict[str, str] = {}
        for event in self.read_events(session_id, tail_limit=tail_limit):
            status = terminal_types.get(event.type)
            if status is not None and event.job_id in job_ids:
                statuses[event.job_id] = status
        return statuses

    def latest_event_cursor(self, session_id: str) -> str | None:
        """读取事件日志尾部的可验证传输游标，供新 SSE 连接跳过历史事件。"""
        with self._file_locks[session_id]:
            file = self._trace_file(session_id)
            latest = self._read_last_event_line(file)
            if latest is None:
                return None
            start, end, line = latest
            try:
                raw_event = json.loads(line.decode("utf-8"))
                event = _AnyEvent.model_validate(raw_event).root
            except Exception as exc:
                raise RuntimeError(
                    f"Trace 尾部事件无法解析: session_id={session_id}"
                ) from exc
        return encode_trace_cursor(
            event_id=event.event_id,
            start=start,
            end=end,
            line=line,
        )

    def read_trace_page(
        self,
        session_id: str,
        *,
        cursor: str | None,
        limit: int,
        max_bytes: int = TRACE_PAGE_MAX_BYTES,
    ) -> TraceEventPage:
        with self._file_locks[session_id]:
            return read_trace_event_page(
                session_id=session_id,
                file=self._trace_file(session_id),
                cursor=cursor,
                limit=limit,
                max_bytes=max_bytes,
            )

    def read_message_events(
        self,
        session_id: str,
        tail_limit: int | None = None,
    ) -> list[Event]:
        return self._read_file_events(
            session_id,
            self._message_trace_file(session_id),
            tail_limit=tail_limit,
        )

    def capture_turn_migration_snapshot(
        self,
        session_id: str,
    ) -> TurnMigrationSnapshot:
        """在一次同步文件锁内捕获语义 Trace 边界和全量事件水位。"""
        with self._file_locks[session_id]:
            message_file = self._message_trace_file(session_id)
            message_trace_size = (
                message_file.stat().st_size if message_file.exists() else 0
            )
            index_snapshot = TraceTurnIndex(
                self._trace_file(session_id).parent
            ).snapshot()
            return TurnMigrationSnapshot(
                message_trace_size=message_trace_size,
                event_cursor=(
                    index_snapshot.event_cursor if index_snapshot is not None else None
                ),
                projected_event_offset=(
                    index_snapshot.projected_message_offset
                    if index_snapshot is not None
                    and index_snapshot.event_cursor is not None
                    else None
                ),
            )

    def iter_message_events(
        self,
        session_id: str,
        *,
        before_offset: int | None = None,
    ) -> Iterator[Event]:
        """逐行读取迁移所需语义事件，避免把完整 Trace 驻留内存。"""
        if before_offset is not None and before_offset < 0:
            raise ValueError("Trace 迁移读取边界不能小于 0")
        file = self._message_trace_file(session_id)
        if not file.exists():
            if before_offset not in {None, 0}:
                raise TypeError(
                    "Trace 语义迁移边界越过不存在的文件: "
                    f"session_id={session_id}, before_offset={before_offset}"
                )
            return
        if before_offset == 0:
            return
        with file.open("rb") as stream:
            line_number = 0
            while line := stream.readline():
                line_number += 1
                current_offset = stream.tell()
                if before_offset is not None and current_offset > before_offset:
                    raise RuntimeError(
                        "Trace 语义迁移边界落在事件行中间: "
                        f"session_id={session_id}, before_offset={before_offset}, "
                        f"line={line_number}"
                    )
                stripped = line.strip().decode("utf-8")
                if not stripped:
                    if before_offset is not None and current_offset == before_offset:
                        return
                    continue
                try:
                    yield self._parse_event(stripped)
                except Exception as error:
                    raise RuntimeError(
                        "Trace 语义迁移事件损坏: "
                        f"session_id={session_id}, line={line_number}"
                    ) from error
                if before_offset is not None and current_offset == before_offset:
                    return
            final_offset = stream.tell()
        if before_offset is not None and final_offset != before_offset:
            raise RuntimeError(
                "Trace 语义迁移边界越过文件末尾: "
                f"session_id={session_id}, before_offset={before_offset}, "
                f"size={final_offset}"
            )

    def _read_file_events(
        self,
        session_id: str,
        file: Path,
        *,
        tail_limit: int | None = None,
    ) -> list[Event]:
        if not file.exists():
            return []

        raw_events: list[dict[str, object]] = []
        lines = (
            self._read_last_lines(file, tail_limit)
            if tail_limit is not None
            else file.read_text(encoding="utf-8").splitlines()
        )
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except Exception as exc:
                raise RuntimeError(
                    f"Trace 行无法解析: session_id={session_id} line={line[:200]!r}"
                ) from exc
            if not isinstance(value, dict):
                raise TypeError(
                    f"Trace 行必须是 JSON object: session_id={session_id} line={line[:200]!r}"
                )
            raw_events.append(value)

        events: list[Event] = []
        for raw_event in raw_events:
            try:
                events.append(_AnyEvent.model_validate(raw_event).root)
            except Exception as exc:
                raise RuntimeError(
                    f"Trace 事件协议无效: session_id={session_id} event={raw_event!r}"
                ) from exc
        return events

    @staticmethod
    def _read_last_lines(file: Path, limit: int) -> list[str]:
        if limit < 1:
            raise ValueError("Trace tail_limit 必须大于 0")
        block_size = 64 * 1024
        chunks: list[bytes] = []
        newline_count = 0
        with file.open("rb") as stream:
            stream.seek(0, 2)
            position = stream.tell()
            while position > 0 and newline_count <= limit:
                read_size = min(block_size, position)
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        raw_lines = b"".join(reversed(chunks)).splitlines()
        return [line.decode("utf-8") for line in raw_lines[-limit:]]

    @staticmethod
    def _read_last_event_line(file: Path) -> tuple[int, int, bytes] | None:
        if not file.exists() or file.stat().st_size == 0:
            return None

        with file.open("rb") as stream:
            file_size = file.stat().st_size
            content_end = file_size
            while content_end > 0:
                stream.seek(content_end - 1)
                if stream.read(1) != b"\n":
                    break
                content_end -= 1
            if content_end == 0:
                return None

            search_end = content_end
            line_start = 0
            while search_end > 0:
                chunk_start = max(0, search_end - 64 * 1024)
                stream.seek(chunk_start)
                chunk = stream.read(search_end - chunk_start)
                newline_index = chunk.rfind(b"\n")
                if newline_index >= 0:
                    line_start = chunk_start + newline_index + 1
                    break
                search_end = chunk_start

            line_end = content_end + (1 if content_end < file_size else 0)
            stream.seek(line_start)
            line = stream.read(line_end - line_start)
        return line_start, line_end, line

    def ensure_cursor(self, session_id: str, after_event_id: str | None) -> None:
        """在响应 SSE 之前验证游标，使失效游标能返回明确的 HTTP 状态。"""
        if after_event_id is None:
            return
        self._offset_after_event(
            session_id, self._trace_file(session_id), after_event_id
        )

    async def stream_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
    ):
        file = self._trace_file(session_id)
        offset = (
            self._offset_after_event(session_id, file, after_event_id)
            if after_event_id is not None
            else 0
        )
        async for record in stream_trace_records(
            session_id=session_id,
            file=file,
            initial_offset=offset,
            requested_cursor=after_event_id,
            condition=self._conditions[session_id],
        ):
            yield record

    async def stream_message_events(
        self, session_id: str
    ) -> AsyncGenerator[Event, None]:
        async for record in stream_trace_records(
            session_id=session_id,
            file=self._message_trace_file(session_id),
            initial_offset=0,
            requested_cursor=None,
            condition=self._conditions[session_id],
        ):
            yield record.event

    def _offset_after_event(
        self, session_id: str, file: Path, after_event_id: str
    ) -> int:
        return offset_after_event(
            session_id,
            file,
            self._trace_file(session_id).parent,
            after_event_id,
        )

    @staticmethod
    def _parse_event(line: str) -> Event:
        return _AnyEvent.model_validate_json(line).root
