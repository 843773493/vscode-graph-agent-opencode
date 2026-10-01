from __future__ import annotations

import asyncio
import zlib

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.schemas.event import Event

# 会话级写入锁按 session_id 分片；分片数固定，避免长驻进程随历史会话数无界增长。
TRACE_RECORDER_LOCK_SHARDS = 64

# Job 已经到达终态后不再发布任何事件，其 job_id -> session_id 映射可以就地回收。
_TERMINAL_JOB_EVENT_TYPES = frozenset(
    {"job_completed", "job_failed", "job_cancelled"}
)


class TraceEventRecorder:
    """将 JobEventBus 的事件同步写入权威会话 trace。"""

    def __init__(
        self,
        *,
        bus: JobEventBusProtocol,
        store: TraceEventSinkProtocol,
    ) -> None:
        self._bus = bus
        self._store = store
        # job_id -> session_id 只服务于「payload 不带 session_id 的进程内事件」，
        # Job 终态后即可回收，上界不随历史 Job 数增长。
        self._job_sessions: dict[str, str] = {}
        self._session_locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(TRACE_RECORDER_LOCK_SHARDS)
        )
        self._started = False

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        # 分片锁池：同一 session_id 恒得同一把锁（保住同会话写入互斥），数量恒为
        # 分片数（有界）。用 crc32 而非 hash() 是为了跨进程确定，不引入 hash 随机化。
        shard = zlib.crc32(session_id.encode("utf-8")) % len(self._session_locks)
        return self._session_locks[shard]

    async def start(self) -> None:
        if self._started:
            return
        await self._bus.register_durable_listener(self._handle_event)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self._bus.unregister_durable_listener(self._handle_event)
        self._started = False

    async def _handle_event(self, event: Event) -> None:
        session_id = self._resolve_session_id(event)
        if not session_id:
            raise RuntimeError(
                "无法持久化缺少 session_id 的事件: "
                f"event_id={event.event_id} type={event.type} job_id={event.job_id}"
            )

        async with self._session_lock(session_id):
            await self._store.append(session_id, event)
            if event.type == "job_created":
                self._job_sessions[event.job_id] = session_id
            elif event.type in _TERMINAL_JOB_EVENT_TYPES:
                # Job 终态：后续事件要么属于别的 job_id，要么自带 session_id，
                # 该映射已无消费者，就地回收避免随历史 Job 数无界增长。
                self._job_sessions.pop(event.job_id, None)

    def _resolve_session_id(self, event: Event) -> str | None:
        payload = event.payload

        if hasattr(payload, "session_id"):
            value = payload.session_id
            if isinstance(value, str) and value:
                return value

        mapped = self._job_sessions.get(event.job_id)
        if mapped:
            return mapped

        return None
