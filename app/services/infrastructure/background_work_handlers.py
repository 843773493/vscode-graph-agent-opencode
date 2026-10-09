"""既有时间消息 producer 的版本化恢复 handler。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.abstractions.background_message_bus import (
    BackgroundMessageDTO,
    BackgroundMessageKind,
)
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)


class SystemTimeEmitterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: float = Field(gt=0)
    message_count: int = Field(gt=0)
    source_id: str = Field(min_length=1)
    started_at: datetime


class SystemTimeEmitterCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next_index: int = Field(ge=0)
    next_due_at: datetime


class SystemTimeEmitterHandler:
    kind = "emit_system_time_messages"
    version = 1
    effect: Literal["atomic_mailbox"] = "atomic_mailbox"

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        payload = SystemTimeEmitterPayload.model_validate(descriptor.payload)
        return SystemTimeEmitterCheckpoint(next_index=0, next_due_at=payload.started_at).model_dump(mode="json")

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        SystemTimeEmitterCheckpoint.model_validate(checkpoint)

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        raise ValueError("时间消息与 checkpoint 原子提交，没有外部效果需要对账")

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        payload = SystemTimeEmitterPayload.model_validate(handle.descriptor.payload)
        checkpoint = SystemTimeEmitterCheckpoint.model_validate(handle.checkpoint)
        if checkpoint.next_index >= payload.message_count:
            raise RuntimeError("时间消息 checkpoint 越过 descriptor 的消息数量")
        await asyncio.sleep(max(0, (checkpoint.next_due_at - datetime.now(UTC)).total_seconds()))
        now = datetime.now(UTC)
        index = checkpoint.next_index
        message = BackgroundMessageDTO(
            message_id=f"{handle.task_id}:message:{index}",
            session_id=handle.session_id, thread_id=handle.thread_id,
            agent_id=handle.agent_id, source_id=payload.source_id,
            kind=BackgroundMessageKind.normal, content=now.isoformat(timespec="seconds"),
            payload={"index": index + 1, "message_count": payload.message_count,
                     "interval_seconds": payload.interval_seconds}, timestamp=now,
        )
        next_checkpoint = SystemTimeEmitterCheckpoint(
            next_index=index + 1, next_due_at=now + timedelta(seconds=payload.interval_seconds),
        )
        return BackgroundTaskStep(
            checkpoint=next_checkpoint.model_dump(mode="json"),
            completed=index + 1 == payload.message_count, messages=(message,),
            result={
                "session_id": handle.session_id, "thread_id": handle.thread_id,
                "agent_id": handle.agent_id, "source_id": payload.source_id,
                "interval_seconds": payload.interval_seconds, "message_count": payload.message_count,
            } if index + 1 == payload.message_count else None,
            result_message_ids=tuple(f"{handle.task_id}:message:{i}" for i in range(index + 1))
            if index + 1 == payload.message_count else (),
        )
