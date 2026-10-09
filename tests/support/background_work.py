"""后台闭包测试的 typed handler 和显式注册 fixture；不使用旧 runner 接口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.background_work_handlers import (
    SystemTimeEmitterHandler,
)
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.orchestration.thread_residency import ThreadResidencyTracker


@dataclass
class ControlledBackgroundHandler:
    kind: str = "controlled_wait"
    version: int = 1
    effect: Literal["atomic_mailbox", "external"] = "atomic_mailbox"
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0
    reconciliation: BackgroundTaskStep | None = None

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        if descriptor.payload != {}:
            raise ValueError("controlled handler descriptor 必须为空")
        return {"done": False}

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        if set(checkpoint) != {"done"} or not isinstance(checkpoint["done"], bool):
            raise ValueError("controlled handler checkpoint 形状非法")

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        return BackgroundTaskStep(checkpoint={"done": True}, completed=True)

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        return self.reconciliation


def build_background_registry(
    sessions_dir: Path, *, handler: ControlledBackgroundHandler | None = None,
    tracker: ThreadResidencyTracker | None = None,
) -> BackgroundTaskRegistry:
    return BackgroundTaskRegistry(
        store=BackgroundWorkStore(sessions_dir=sessions_dir),
        residency_tracker=tracker if tracker is not None else ThreadResidencyTracker(),
        handlers=(handler if handler is not None else SystemTimeEmitterHandler(),),
    )


async def spawn_waiting_task(
    registry: BackgroundTaskRegistry, sessions_dir: Path, session_id: str,
    *, task_id: str = "bgt_waiting",
) -> BackgroundTaskHandle:
    return await registry.spawn(
        session_id=session_id,
        thread_id=get_session_path_resolver(sessions_dir).main_thread_id(session_id),
        agent_id="default", task_id=task_id,
        descriptor=BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
            "interval_seconds": 60, "message_count": 2, "source_id": "waiting",
            "started_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        }),
    )
