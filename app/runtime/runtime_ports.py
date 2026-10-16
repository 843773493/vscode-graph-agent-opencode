"""Backend 启动时注入的独立时钟；默认生产时钟，不读取测试环境。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.business.communication.wait import DeadlineTimer, WaitClock


@dataclass(frozen=True, slots=True)
class RuntimeClockPorts:
    residency: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)
    wait: WaitClock | None = None
    deadline_timer: DeadlineTimer | None = None


DEFAULT_RUNTIME_CLOCK_PORTS = RuntimeClockPorts()
