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


class TraceEventStore:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    def _trace_file(self, session_id: str) -> Path:
        return self._logs_dir / "traces" / f"trace_{session_id}.jsonl"

    def append(self, session_id: str, event: Event) -> None:
        file = self._trace_file(session_id)
        file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(file, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
        except Exception as exc:
            logger.exception("写入 trace 文件失败: session_id=%s event_id=%s", session_id, event.event_id)
            raise

        condition = self._conditions.get(session_id)
        if condition is None:
            return

        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(condition.notify_all)
        except RuntimeError:
            pass

    def read_events(self, session_id: str) -> list[Event]:
        file = self._trace_file(session_id)
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
                    logger.exception("解析 trace 事件失败: session_id=%s line=%r", session_id, line[:200])
                    raise
        return events

    async def stream_events(self, session_id: str) -> AsyncGenerator[Event, None]:
        seen = 0
        condition = self._conditions[session_id]

        while True:
            events = self.read_events(session_id)
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
