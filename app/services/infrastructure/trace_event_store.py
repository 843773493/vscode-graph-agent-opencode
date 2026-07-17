from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import AsyncGenerator

from pydantic import RootModel

from app.schemas.event import Event

logger = logging.getLogger(__name__)


class _AnyEvent(RootModel[Event]):
    pass


# 关键对话消息 trace 中保留的事件类型：用户任务创建、模型文本结束、工具调用与响应。
MESSAGE_TRACE_TYPES = frozenset({"job_created", "text_end", "tool_call_start", "tool_call_end"})


class TraceCursorGoneError(RuntimeError):
    """请求的事件游标已不在当前 trace 文件中。"""

    def __init__(self, session_id: str, event_id: str) -> None:
        self.session_id = session_id
        self.event_id = event_id
        super().__init__(f"Trace 事件游标不存在: session_id={session_id}, event_id={event_id}")


class TraceEventStore:
    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._append_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _trace_file(self, session_id: str) -> Path:
        return self._sessions_dir / session_id / "logs" / "traces" / "events.jsonl"

    def _message_trace_file(self, session_id: str) -> Path:
        return self._sessions_dir / session_id / "logs" / "traces" / "messages.jsonl"

    async def _notify(self, session_id: str) -> None:
        condition = self._conditions.get(session_id)
        if condition is None:
            return
        async with condition:
            condition.notify_all()

    def _append_to_file(self, session_id: str, file: Path, event: Event) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(file, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
        except Exception:
            logger.exception("写入 trace 文件失败: session_id=%s event_id=%s", session_id, event.event_id)
            raise

    def _append_event_files(self, session_id: str, event: Event) -> None:
        self._append_to_file(session_id, self._trace_file(session_id), event)

        if event.type in MESSAGE_TRACE_TYPES:
            self._append_to_file(session_id, self._message_trace_file(session_id), event)

    async def append(self, session_id: str, event: Event) -> None:
        async with self._append_locks[session_id]:
            await asyncio.to_thread(self._append_event_files, session_id, event)
        await self._notify(session_id)

    def read_events(self, session_id: str, after_event_id: str | None = None) -> list[Event]:
        events = self._read_file_events(session_id, self._trace_file(session_id))
        return self._events_after_cursor(session_id, events, after_event_id)

    def read_message_events(self, session_id: str) -> list[Event]:
        return self._read_file_events(session_id, self._message_trace_file(session_id))

    def _read_file_events(self, session_id: str, file: Path) -> list[Event]:
        if not file.exists():
            return []

        raw_events: list[dict[str, object]] = []
        with open(file, "r", encoding="utf-8") as f:
            for line in f:
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
                    raise RuntimeError(
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
    def _events_after_cursor(
        session_id: str,
        events: list[Event],
        after_event_id: str | None,
    ) -> list[Event]:
        if after_event_id is None:
            return events
        for index, event in enumerate(events):
            if event.event_id == after_event_id:
                return events[index + 1 :]
        raise TraceCursorGoneError(session_id, after_event_id)

    def ensure_cursor(self, session_id: str, after_event_id: str | None) -> None:
        """在响应 SSE 之前验证游标，使失效游标能返回明确的 HTTP 状态。"""
        if after_event_id is None:
            return
        self._offset_after_event(session_id, self._trace_file(session_id), after_event_id)

    async def stream_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
    ) -> AsyncGenerator[Event, None]:
        async for event in self._stream_file_events(
            session_id,
            self._trace_file(session_id),
            after_event_id,
        ):
            yield event

    async def stream_message_events(self, session_id: str) -> AsyncGenerator[Event, None]:
        async for event in self._stream_file_events(session_id, self._message_trace_file(session_id)):
            yield event

    def _offset_after_event(self, session_id: str, file: Path, after_event_id: str) -> int:
        if not file.exists():
            raise TraceCursorGoneError(session_id, after_event_id)

        with open(file, "rb") as stream:
            while line := stream.readline():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw_event = json.loads(stripped.decode("utf-8"))
                except Exception as exc:
                    raise RuntimeError(
                        f"Trace 游标扫描遇到损坏行: session_id={session_id} "
                        f"line={stripped[:200]!r}"
                    ) from exc
                if isinstance(raw_event, dict) and raw_event.get("event_id") == after_event_id:
                    return stream.tell()
        raise TraceCursorGoneError(session_id, after_event_id)

    async def _stream_file_events(
        self,
        session_id: str,
        file: Path,
        after_event_id: str | None = None,
    ) -> AsyncGenerator[Event, None]:
        offset = (
            self._offset_after_event(session_id, file, after_event_id)
            if after_event_id is not None
            else 0
        )
        condition = self._conditions[session_id]

        while True:
            if file.exists():
                if file.stat().st_size < offset:
                    cursor = after_event_id or "<stream-offset>"
                    raise TraceCursorGoneError(session_id, cursor)
                with open(file, "rb") as stream:
                    stream.seek(offset)
                    raw_events: list[dict[str, object]] = []
                    while line := stream.readline():
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
                            raise RuntimeError(
                                f"Trace 流事件必须是 JSON object: session_id={session_id}"
                            )
                        raw_events.append(raw_event)

                    for raw_event in raw_events:
                        try:
                            yield _AnyEvent.model_validate(raw_event).root
                        except Exception as exc:
                            raise RuntimeError(
                                "Trace 流事件协议无效: "
                                f"session_id={session_id} event={raw_event!r}"
                            ) from exc

            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _parse_event(line: str) -> Event:
        return _AnyEvent.model_validate_json(line).root
