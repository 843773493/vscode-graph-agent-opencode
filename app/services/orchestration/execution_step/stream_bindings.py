"""Agent step 的消息事件发布与 canonical item sink 绑定。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from app.domain.itemized.records import CanonicalItemRecord


class StepEventPublisher:
    """为一个 step 固定 job/agent 身份，防止事件串流。"""

    def __init__(self, bus: Any, *, job_id: str, agent_id: str) -> None:
        self._bus = bus
        self._job_id = job_id
        self._agent_id = agent_id

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(
            job_id=self._job_id,
            event_type=event_type,
            payload=payload,
            agent_id=self._agent_id,
        )


class CanonicalItemSink:
    """把实时 canonical item draft 交给 Saver-owned v2 append API。"""

    def __init__(
        self,
        checkpointer: object,
        *,
        session_id: str,
        checkpoint_ns: str,
    ) -> None:
        append_items = getattr(checkpointer, "append_items", None)
        if not callable(append_items):
            raise TypeError("canonical item sink 必须绑定 Saver append_items 端口")
        self._append_items = append_items
        self._session_id = session_id
        self._checkpoint_ns = checkpoint_ns

    async def append(self, items: Sequence[object]) -> None:
        typed_items = tuple(
            item for item in items if isinstance(item, CanonicalItemRecord)
        )
        if len(typed_items) != len(items):
            raise TypeError("canonical item sink 收到非法 item")
        await asyncio.to_thread(
            self._append_items,
            self._session_id,
            typed_items,
            checkpoint_ns=self._checkpoint_ns,
        )


__all__ = ["CanonicalItemSink", "StepEventPublisher"]
