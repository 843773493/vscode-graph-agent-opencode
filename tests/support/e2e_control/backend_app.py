"""显式测试 ASGI composition；生产入口没有测试环境或测试路由。"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import cast

# 只观察本显式测试 composition 的 import；进入本模块前的 Python/uvicorn 不在覆盖范围。
faulthandler.dump_traceback_later(55, repeat=False)

from app.core.identifier import bind_identifier_factory
from app.core.phase_observer import bind_phase_observer
from app.runtime.runtime_ports import RuntimeClockPorts
from tests.support.e2e_control.harness import control_request

_address = json.loads(os.environ["BOXTEAM_E2E_TEST_CONTROL"])


async def _request(payload: dict[str, object]) -> object:
    reader, writer = await asyncio.open_connection(_address["host"], _address["port"])
    try:
        writer.write(json.dumps({**payload, "token": _address["token"], "pid": os.getpid()}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["value"]
    finally:
        writer.close()
        await writer.wait_closed()


class ControlledIdentifiers:
    def create(self, *, namespace: str, epoch_ms: int | None) -> str:
        return cast(str, control_request(_address, {"action": "identifier", "namespace": namespace, "epoch_ms": epoch_ms}))


class ControlledPhases:
    async def reached(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        await _request({"action": "phase", "operation_id": operation_id, "phase": phase, "details": details})

    def reached_sync(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        control_request(_address, {"action": "phase", "operation_id": operation_id, "phase": phase, "details": details})


class ControlledClock:
    def _snapshot(self) -> dict[str, object]:
        return cast(dict[str, object], control_request(_address, {"action": "clock"}))

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
    async def wait(self, signal: Awaitable[object], timeout: float | None) -> None:
        if timeout is None:
            await signal
            return
        snapshot = _clock._snapshot()
        notified = asyncio.ensure_future(signal)
        expired = asyncio.create_task(_request({"action": "deadline", "origin": snapshot["origin"], "deadline_ns": cast(int, snapshot["wait_ns"]) + int(timeout * 1_000_000_000)}))
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


bind_identifier_factory(ControlledIdentifiers())
bind_phase_observer(ControlledPhases())
_clock = ControlledClock()

# 测试装配先绑定 port，再导入既有应用与真实 lifespan，不创建第二应用。
from app.main import app

faulthandler.cancel_dump_traceback_later()
app.state.clock_ports = RuntimeClockPorts(residency=_clock.residency, wall=_clock.utc_now, wait=_clock, deadline_timer=ControlledDeadlineTimer())
