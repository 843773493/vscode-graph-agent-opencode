"""显式控制地址的 ID、phase 和时钟端口；导入时不绑定应用或读取环境。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import cast

from tests.support.e2e_control.harness import control_request


async def _request(address: dict[str, object], payload: dict[str, object]) -> object:
    reader, writer = await asyncio.open_connection(cast(str, address["host"]), cast(int, address["port"]))
    try:
        writer.write(json.dumps({**payload, "token": address["token"], "pid": os.getpid()}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["value"]
    finally:
        writer.close()
        await writer.wait_closed()


class ControlledIdentifiers:
    def __init__(self, address: dict[str, object]) -> None:
        self._address = address

    def create(self, *, namespace: str, epoch_ms: int | None) -> str:
        return cast(str, control_request(self._address, {"action": "identifier", "namespace": namespace, "epoch_ms": epoch_ms}))


class ControlledPhases:
    def __init__(self, address: dict[str, object]) -> None:
        self._address = address

    async def reached(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        await _request(self._address, {"action": "phase", "operation_id": operation_id, "phase": phase, "details": details})

    def reached_sync(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        control_request(self._address, {"action": "phase", "operation_id": operation_id, "phase": phase, "details": details})


class ControlledClock:
    def __init__(self, address: dict[str, object]) -> None:
        self._address = address

    def _snapshot(self) -> dict[str, object]:
        return cast(dict[str, object], control_request(self._address, {"action": "clock"}))

    @property
    def origin_id(self) -> str:
        return cast(str, self._snapshot()["origin"])

    def monotonic_ns(self) -> int:
        return cast(int, self._snapshot()["wait_ns"])

    def utc_now(self) -> datetime:
        return datetime.fromisoformat(cast(str, self._snapshot()["wait_utc"]))

    def residency(self) -> float:
        return cast(float, self._snapshot()["residency_seconds"])


class ControlledDeadlineTimer:
    def __init__(self, address: dict[str, object], clock: ControlledClock) -> None:
        self._address = address
        self._clock = clock

    async def wait(self, signal: Awaitable[object], timeout: float | None) -> None:
        if timeout is None:
            await signal
            return
        snapshot = self._clock._snapshot()
        notified = asyncio.ensure_future(signal)
        expired = asyncio.create_task(_request(self._address, {"action": "deadline", "origin": snapshot["origin"], "deadline_ns": cast(int, snapshot["wait_ns"]) + int(timeout * 1_000_000_000)}))
        try:
            done, _pending = await asyncio.wait((notified, expired), return_when=asyncio.FIRST_COMPLETED)
            if notified in done:
                await notified
            else:
                await expired
                raise TimeoutError
        finally:
            for task in (notified, expired):
                if not task.done():
                    task.cancel()
            await asyncio.gather(notified, expired, return_exceptions=True)
