from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx


async def read_sse_events_until(
    response: httpx.Response,
    predicate: Callable[[dict[str, object]], bool],
    timeout_seconds: float = 30.0,
) -> list[dict[str, object]]:
    """读取单行 JSON SSE，匹配或流结束时返回；超时与非法事件直接报错。"""

    events: list[dict[str, object]] = []
    async with asyncio.timeout(timeout_seconds):
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue

            raw = line.removeprefix("data:").strip()
            if not raw:
                continue

            event = json.loads(raw)
            if not isinstance(event, dict):
                raise TypeError(f"SSE 测试事件必须是 JSON 对象，实际为 {type(event).__name__}")
            events.append(event)
            if predicate(event):
                return events

    return events
