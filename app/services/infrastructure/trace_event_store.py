from __future__ import annotations

import asyncio
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


class TraceEventStore:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    def _trace_file(self, session_id: str) -> Path:
        return self._logs_dir / "traces" / f"trace_{session_id}.jsonl"

    def _message_trace_file(self, session_id: str) -> Path:
        return self._logs_dir / "traces" / f"trace_message_{session_id}.jsonl"

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

    def append(self, session_id: str, event: Event) -> None:
        self._append_to_file(session_id, self._trace_file(session_id), event)

        if event.type in MESSAGE_TRACE_TYPES:
            self._append_to_file(session_id, self._message_trace_file(session_id), event)

        try:
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(self._notify(session_id), loop)
        except RuntimeError:
            pass

    def read_events(self, session_id: str) -> list[Event]:
        return self._read_file_events(session_id, self._trace_file(session_id))

    def read_message_events(self, session_id: str) -> list[Event]:
        return self._read_file_events(session_id, self._message_trace_file(session_id))

    def _read_file_events(self, session_id: str, file: Path) -> list[Event]:
        if not file.exists():
            return []

        events: list[Event] = []
        with open(file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(self._parse_event(line))
                except Exception as exc:
                    logger.warning("跳过无法解析的 trace 行: session_id=%s error=%s line=%r", session_id, exc, line[:200])
                    continue
        return events

    async def stream_events(self, session_id: str) -> AsyncGenerator[Event, None]:
        async for event in self._stream_file_events(session_id, self._trace_file(session_id)):
            yield event

    async def stream_message_events(self, session_id: str) -> AsyncGenerator[Event, None]:
        async for event in self._stream_file_events(session_id, self._message_trace_file(session_id)):
            yield event

    async def _stream_file_events(self, session_id: str, file: Path) -> AsyncGenerator[Event, None]:
        seen = 0
        condition = self._conditions[session_id]

        while True:
            events = self._read_file_events(session_id, file)
            new_events = events[seen:]
            for event in new_events:
                yield event
            seen = len(events)

            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _parse_event(line: str) -> Event:
        return _AnyEvent.model_validate_json(line).root
