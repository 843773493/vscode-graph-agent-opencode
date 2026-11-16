from __future__ import annotations

import asyncio
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import aclosing
from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.event import Event
from app.schemas.internal_v2.trace_search import TraceLocationDTO, TraceReferenceDTO
from app.services.infrastructure.events.event_channel_service import (
    SESSION_TRACE_CHANNEL_KIND,
    EventChannelService,
    EventChannelSpec,
    channel_name,
)
from app.services.infrastructure.session_trace.trace_cursor import (
    TraceStreamRecord,
    encode_trace_cursor,
    events_after_cursor,
    offset_after_event,
)
from app.services.infrastructure.session_trace.trace_database import (
    TraceDatabase,
    job_threads,
)
from app.services.infrastructure.session_trace.trace_page import (
    TRACE_PAGE_MAX_BYTES,
    TraceEventPage,
    parse_trace_record,
    read_trace_event_page,
)
from app.services.infrastructure.session_trace.trace_search import (
    TraceSearchBudget,
    TraceSearchPage,
    TraceSearchPosition,
    locate_trace_event,
    search_trace_events,
)
from app.services.infrastructure.session_trace.trace_stream import (
    iter_trace_event_lines,
    stream_trace_records,
)

__all__ = ["TraceEventStore"]
TRACE_STORE_LOCK_SHARDS = 64


class TraceEventStore:
    def __init__(self, sessions_dir: Path, *, event_service: EventChannelService | None = None) -> None:
        self._path_resolver = get_session_path_resolver(sessions_dir)
        self._append_locks = tuple(
            asyncio.Lock() for _ in range(TRACE_STORE_LOCK_SHARDS)
        )
        self._event_service = event_service if event_service is not None else EventChannelService()

    @staticmethod
    def _shard(session_id: str) -> int:
        return zlib.crc32(session_id.encode("utf-8")) % TRACE_STORE_LOCK_SHARDS

    def _append_lock(self, session_id: str) -> asyncio.Lock:
        return self._append_locks[self._shard(session_id)]

    @staticmethod
    def _channel_spec(session_id: str, thread_id: str | None) -> EventChannelSpec:
        return EventChannelSpec(
            name=channel_name(SESSION_TRACE_CHANNEL_KIND, f"{session_id}:{thread_id or '*'}"),
            overflow_policy="fail_closed", history_size=0,
        )

    def _database_path(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node_for_runtime(session_id)
            / "logs"
            / "traces"
            / "trace.sqlite"
        )

    def list_session_ids(self) -> list[str]:
        return sorted(
            node.node_id for node in self._path_resolver.list_nodes(kind="session")
        )

    def _append_event(self, session_id: str, event: Event) -> TraceStreamRecord:
        return TraceDatabase(self._database_path(session_id)).append(event)

    async def append(self, session_id: str, event: Event) -> None:
        async with self._append_lock(session_id):
            record = await run_sync_shielded(self._append_event, session_id, event)
            # 与提交共用顺序锁；无人订阅时不创建 channel，也不保留正文副本。
            for scope in {None, record.thread_id}:
                channel = self._event_service.find_channel(self._channel_spec(session_id, scope).name)
                if channel is not None:
                    channel.publish(record)
                    self._event_service.remove_channel_if_unused(channel.spec.name)

    def read_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
        tail_limit: int | None = None,
    ) -> list[Event]:
        events = list(
            self.iter_events(
                session_id, tail_limit=tail_limit if after_event_id is None else None
            )
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
            # 后端进程重启时没有可安全续接的 AgentLoop；它是失败终态，
            # 不是用户主动取消，历史回放和 retry_failed 需要保留这个区别。
            "session_interrupted": "failed",
        }
        statuses: dict[str, str] = {}
        for event in self.read_events(session_id, tail_limit=tail_limit):
            status = terminal_types.get(event.type)
            if status is not None and event.job_id in job_ids:
                statuses[event.job_id] = status
        return statuses

    def latest_event_cursor(self, session_id: str) -> str | None:
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            row = (
                connection.execute(
                    "SELECT start,end,raw,event_id FROM trace_events ORDER BY start DESC LIMIT 1"
                ).fetchone()
                if connection
                else None
            )
            if row is None:
                return None
            record = parse_trace_record(
                session_id, row[0], row[1], row[2].encode("utf-8")
            )
            return encode_trace_cursor(
                event_id=record.event.event_id,
                start=record.start,
                end=record.end,
                line=record.line,
            )

    def read_trace_page(
        self,
        session_id: str,
        *,
        cursor: str | None,
        limit: int,
        max_bytes: int = TRACE_PAGE_MAX_BYTES,
        thread_id: str | None = None,
    ) -> TraceEventPage:
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            return read_trace_event_page(
                session_id=session_id,
                connection=connection,
                cursor=cursor,
                limit=limit,
                max_bytes=max_bytes,
                thread_id=thread_id,
            )

    def event_threads(self, session_id: str, events: list[Event]) -> dict[str, str]:
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            return self._event_threads(connection, session_id, events)

    def _event_threads(
        self,
        connection: sqlite3.Connection | None,
        session_id: str,
        events: list[Event],
    ) -> dict[str, str]:
        jobs = {
            event.job_id
            for event in events
            if event.type not in {"goal_updated", "goal_cleared"}
        }
        owners = job_threads(connection, session_id, jobs)
        result: dict[str, str] = {}
        main: str | None = None
        for event in events:
            if event.type in {"goal_updated", "goal_cleared"}:
                if main is None:
                    main = self._path_resolver.main_thread_id(session_id)
                if (
                    event.payload.session_id != session_id
                    or event.payload.thread_id != main
                ):
                    raise ValueError("Goal 事件归属与 Session main Thread 不一致")
                result[event.event_id] = main
            else:
                result[event.event_id] = owners[event.job_id]
        return result

    def search_session_events(
        self,
        session_id: str,
        query: str,
        *,
        limit: int,
        position: TraceSearchPosition | None,
        budget: TraceSearchBudget,
    ) -> TraceSearchPage:
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            return search_trace_events(
                session_id,
                connection,
                query,
                limit=limit,
                position=position,
                budget=budget,
                event_threads=lambda events: self._event_threads(
                    connection, session_id, events
                ),
            )

    def locate(self, reference: TraceReferenceDTO) -> TraceLocationDTO:
        with TraceDatabase(
            self._database_path(reference.session_id)
        ).connection() as connection:
            return locate_trace_event(
                connection,
                reference,
                event_threads=lambda events: self._event_threads(
                    connection, reference.session_id, events
                ),
            )

    def iter_message_events(self, session_id: str) -> Iterator[Event]:
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            if connection is not None:
                rows = connection.execute(
                    "SELECT raw FROM trace_events WHERE is_message=1 ORDER BY start"
                )
                yield from iter_trace_event_lines(
                    session_id=session_id, lines=(row[0] for row in rows)
                )

    def iter_events(
        self, session_id: str, *, tail_limit: int | None = None
    ) -> Iterator[Event]:
        """恢复必须耗尽快照中的全部事件，完整协议与归属校验仍由原调用方执行。"""
        if tail_limit is not None and tail_limit < 1:
            raise ValueError("Trace tail_limit 必须大于 0")
        with TraceDatabase(self._database_path(session_id)).connection() as connection:
            if connection is None:
                return
            if tail_limit is None:
                rows = connection.execute("SELECT raw FROM trace_events ORDER BY start")
            else:
                rows = connection.execute(
                    "SELECT raw FROM (SELECT start,raw FROM trace_events ORDER BY start DESC LIMIT ?) ORDER BY start",
                    (tail_limit,),
                )
            yield from iter_trace_event_lines(
                session_id=session_id, lines=(row[0] for row in rows)
            )

    def ensure_cursor(self, session_id: str, after_event_id: str | None) -> None:
        if after_event_id is not None:
            with TraceDatabase(
                self._database_path(session_id)
            ).connection() as connection:
                offset_after_event(session_id, connection, after_event_id)

    async def stream_events(self, session_id: str, after_event_id: str | None = None, *, thread_id: str | None = None):
        async with aclosing(stream_trace_records(
            session_id=session_id,
            database=TraceDatabase(self._database_path(session_id)),
            requested_cursor=after_event_id,
            event_service=self._event_service,
            channel_spec=self._channel_spec(session_id, thread_id),
            thread_id=thread_id,
        )) as records:
            async for record in records:
                yield record
