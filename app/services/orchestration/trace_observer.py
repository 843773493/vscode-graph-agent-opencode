"""实时 stream trace observer 与 runtime observer mixin。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.job_event_bus import EventType


class StreamTraceObserverMixin:
    """把实时事件交给唯一的 normalized-block observer。"""

    async def _observe(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._normalized_block_observer is not None:
            await self._normalized_block_observer(event_type, payload)


class MessageStreamTraceObserver:
    """将已提交的规范化 block 投影为旧 Trace 的诊断事件。

    该投影只服务事件/诊断视图，聊天主时间线不再消费这些事件。
    """

    _TEXT_CARRIERS = frozenset({"text", "output_text", "refusal"})
    _REASONING_CARRIERS = frozenset(
        {"reasoning", "reasoning_content", "thinking"}
    )

    def __init__(self, publish: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._publish = publish
        self._parts: dict[str, dict[str, object]] = {}

    async def observe(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        if event_type == "block.started":
            carrier_type = str(payload.get("carrier_type") or "text")
            kind = self._trace_kind(carrier_type)
            if kind is None:
                return
            block_id = self._required_block_id(payload)
            self._parts[block_id] = {
                "kind": kind,
                "text": "",
                "carrier_type": carrier_type,
                "content_block_index": payload.get("block_index", 0),
            }
            await self._publish(
                EventType.TEXT_START,
                self._trace_payload(block_id, self._parts[block_id]),
            )
            return

        block_id = self._required_block_id(payload)
        part = self._parts.get(block_id)
        if part is None:
            return
        if event_type == "block.delta":
            if payload.get("operation") != "append":
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                return
            part["text"] = str(part.get("text") or "") + text
            await self._publish(
                EventType.TEXT_DELTA,
                {
                    **self._trace_payload(block_id, part),
                    "text": text,
                },
            )
            return
        if event_type == "block.completed":
            await self._publish(
                EventType.TEXT_END,
                {
                    **self._trace_payload(block_id, part),
                    "text": str(part.get("text") or ""),
                },
            )
            del self._parts[block_id]

    @classmethod
    def _trace_kind(cls, carrier_type: str) -> str | None:
        if carrier_type in cls._TEXT_CARRIERS:
            return "markdown"
        if carrier_type in cls._REASONING_CARRIERS:
            return "reasoning"
        return None

    @staticmethod
    def _required_block_id(payload: Mapping[str, object]) -> str:
        block_id = payload.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            raise RuntimeError("规范化 block 事件缺少 block_id")
        return block_id

    @staticmethod
    def _trace_payload(
        block_id: str,
        part: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "part_id": block_id,
            "kind": part["kind"],
            "carrier_type": part["carrier_type"],
            "content_block_index": part["content_block_index"],
        }

__all__ = ["MessageStreamTraceObserver"]
