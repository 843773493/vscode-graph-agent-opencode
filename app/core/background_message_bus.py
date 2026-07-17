from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Set

from app.abstractions.background_message_bus import (
    BackgroundMessageBatchDTO,
    BackgroundMessageDTO,
    BackgroundMessageKind,
)
from app.core.identifier import create_prefixed_id


class BackgroundMessageBus:
    def __init__(self):
        self._messages: Dict[tuple[str, str], Deque[BackgroundMessageDTO]] = {}
        self._subscribers: Dict[tuple[str, str], Set[asyncio.Queue[BackgroundMessageDTO]]] = {}
        self._max_history = 1000

    def _key(self, session_id: str, agent_id: str) -> tuple[str, str]:
        return session_id, agent_id

    async def subscribe(self, session_id: str, agent_id: str) -> asyncio.Queue[BackgroundMessageDTO]:
        queue: asyncio.Queue[BackgroundMessageDTO] = asyncio.Queue(maxsize=100)
        key = self._key(session_id, agent_id)
        if key not in self._subscribers:
            self._subscribers[key] = set()
        self._subscribers[key].add(queue)
        return queue

    async def unsubscribe(self, session_id: str, agent_id: str, queue: asyncio.Queue[BackgroundMessageDTO]) -> None:
        key = self._key(session_id, agent_id)
        if key in self._subscribers:
            self._subscribers[key].discard(queue)
            if not self._subscribers[key]:
                del self._subscribers[key]

    async def list_messages(
        self,
        session_id: str,
        agent_id: str,
        *,
        source_id: str | None = None,
        after_message_id: str | None = None,
        limit: int = 100,
    ) -> list[BackgroundMessageDTO]:
        key = self._key(session_id, agent_id)
        messages = list(self._messages.get(key, []))

        if source_id is not None:
            messages = [message for message in messages if message.source_id == source_id]

        if after_message_id:
            for index, message in enumerate(messages):
                if message.message_id == after_message_id:
                    messages = messages[index + 1 :]
                    break

        return messages[-limit:]

    def emit(
        self,
        session_id: str,
        agent_id: str,
        content: str,
        *,
        kind: BackgroundMessageKind | str = BackgroundMessageKind.normal,
        source_id: str | None = None,
        payload: dict | None = None,
        message_id: str | None = None,
    ) -> BackgroundMessageDTO:
        if not content:
            raise ValueError("content 不能为空")

        normalized_kind = BackgroundMessageKind(kind)
        message = BackgroundMessageDTO(
            message_id=message_id or create_prefixed_id("bgm"),
            session_id=session_id,
            agent_id=agent_id,
            source_id=source_id or create_prefixed_id("src"),
            kind=normalized_kind,
            content=content,
            payload=payload or {},
            timestamp=datetime.now(timezone.utc),
        )

        key = self._key(session_id, agent_id)
        if key not in self._messages:
            self._messages[key] = deque(maxlen=self._max_history)
        self._messages[key].append(message)

        for queue in list(self._subscribers.get(key, set())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

        return message

    async def collect(
        self,
        session_id: str,
        agent_id: str,
        *,
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
    ) -> BackgroundMessageBatchDTO:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        collected: list[BackgroundMessageDTO] = []
        last_message_id = after_message_id
        seen_message_ids: set[str] = set()

        queue = await self.subscribe(session_id, agent_id)

        try:
            backlog = await self.list_messages(
                session_id,
                agent_id,
                source_id=source_id,
                after_message_id=after_message_id,
                limit=self._max_history,
            )

            for message in backlog:
                if message.message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message.message_id)
                collected.append(message)
                last_message_id = message.message_id
                if stop_on_interrupt and message.kind == BackgroundMessageKind.interrupt:
                    return BackgroundMessageBatchDTO(
                        session_id=session_id,
                        agent_id=agent_id,
                        source_id=source_id,
                        messages=collected,
                        interrupted=True,
                        timed_out=False,
                        last_message_id=last_message_id,
                        collected_at=datetime.now(),
                    )

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return BackgroundMessageBatchDTO(
                        session_id=session_id,
                        agent_id=agent_id,
                        source_id=source_id,
                        messages=collected,
                        interrupted=False,
                        timed_out=True,
                        last_message_id=last_message_id,
                        collected_at=datetime.now(),
                    )

                try:
                    message = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return BackgroundMessageBatchDTO(
                        session_id=session_id,
                        agent_id=agent_id,
                        source_id=source_id,
                        messages=collected,
                        interrupted=False,
                        timed_out=True,
                        last_message_id=last_message_id,
                        collected_at=datetime.now(),
                    )

                if source_id is not None and message.source_id != source_id:
                    continue
                if message.message_id in seen_message_ids:
                    continue

                seen_message_ids.add(message.message_id)
                collected.append(message)
                last_message_id = message.message_id

                if stop_on_interrupt and message.kind == BackgroundMessageKind.interrupt:
                    return BackgroundMessageBatchDTO(
                        session_id=session_id,
                        agent_id=agent_id,
                        source_id=source_id,
                        messages=collected,
                        interrupted=True,
                        timed_out=False,
                        last_message_id=last_message_id,
                        collected_at=datetime.now(),
                    )
        finally:
            await self.unsubscribe(session_id, agent_id, queue)


def emit_background_message(
    content: str,
    *,
    kind: BackgroundMessageKind | str = BackgroundMessageKind.normal,
    source_id: str | None = None,
    payload: dict | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    message_id: str | None = None,
) -> BackgroundMessageDTO:
    if not session_id:
        raise RuntimeError("session_id 不能为空，必须显式传入")
    if not agent_id:
        raise RuntimeError("agent_id 不能为空，必须显式传入")

    raise RuntimeError("BackgroundMessageBus 需要通过应用容器显式注入，不能直接调用 emit_background_message")


def emit_interrupt_background_message(
    content: str,
    *,
    source_id: str | None = None,
    payload: dict | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    message_id: str | None = None,
) -> BackgroundMessageDTO:
    return emit_background_message(
        content,
        kind=BackgroundMessageKind.interrupt,
        source_id=source_id,
        payload=payload,
        session_id=session_id,
        agent_id=agent_id,
        message_id=message_id,
    )
