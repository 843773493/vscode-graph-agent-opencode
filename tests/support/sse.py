from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx


async def read_sse_events_until(
    response: httpx.Response,
    predicate: Callable[[dict], bool],
    timeout_seconds: float = 30.0,
) -> list[dict]:
    """读取 SSE 事件，直到 predicate 成立或到达超时时间。"""

    events: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async for line in response.aiter_lines():
        if asyncio.get_running_loop().time() >= deadline:
            break

        line = line.strip()
        if not line or line.startswith((":", "event:")):
            continue

        if line.startswith("data:"):
            raw = line.removeprefix("data:").strip()
            if raw:
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue

        if events and predicate(events[-1]):
            break

    return events
