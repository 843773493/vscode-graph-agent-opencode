"""任务只通过已有有界 channel 推送轻量失效通知，查询仍回到原账本。"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.infrastructure.events.event_channel_service import (
    EventChannel,
    EventChannelService,
    EventChannelSpec,
    channel_name,
)


@dataclass(frozen=True, slots=True)
class PersistentTaskChanged:
    task_id: str
    revision: int
    run_id: str | None = None


def task_channel(events: EventChannelService, workspace_id: str) -> EventChannel[PersistentTaskChanged]:
    return events.ensure_channel(EventChannelSpec(
        name=channel_name("task.lifecycle", workspace_id), overflow_policy="gap", max_queue_size=64,
    ))
