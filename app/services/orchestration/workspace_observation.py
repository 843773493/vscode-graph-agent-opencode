"""并行驱动独立观察者，只合并物理传输，不接管领域状态。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.internal_v2.workspace_observation import WorkspaceObservationFrameDTO

logger = logging.getLogger(__name__)


class WorkspaceObservationError(Exception):
    def __init__(self, status: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class OpenedWorkspaceObservation:
    stream: AsyncIterator[str]
    # connected 信封的逻辑续读水位：files 事件序号十进制字符串，其他 source 为 None。
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceObservationSource:
    subscription_id: str
    open: Callable[[], Awaitable[OpenedWorkspaceObservation]]


async def stream_workspace_observations(
    sources: list[WorkspaceObservationSource],
) -> AsyncIterator[WorkspaceObservationFrameDTO]:
    # 有界队列让慢消费者反压各 source，不累积无界传输副本。
    queue: asyncio.Queue[WorkspaceObservationFrameDTO] = asyncio.Queue(maxsize=100)

    async def pump(source: WorkspaceObservationSource) -> None:
        stage = "open"
        try:
            opened = await source.open()
            async with aclosing(opened.stream) as stream:
                await queue.put(WorkspaceObservationFrameDTO(
                    subscription_id=source.subscription_id,
                    state="connected",
                    cursor=opened.cursor,
                ))
                stage = "stream"
                async for frame in stream:
                    await queue.put(WorkspaceObservationFrameDTO(
                        subscription_id=source.subscription_id,
                        state="frame",
                        frame=frame,
                    ))
        except Exception as error:
            if isinstance(error, WorkspaceObservationError):
                status, detail = error.status, error.detail
            else:
                logger.exception("Workspace 观察者失败: subscription=%s stage=%s", source.subscription_id, stage)
                # 未声明的异常文本可能携带物理 locator；错误信封保留诊断字段，完整原因留在日志。
                status, detail = 500, {
                    "owner": "workspace_observation", "stage": stage,
                    "type": type(error).__name__, "errno": error.errno if isinstance(error, OSError) else None,
                }
            await queue.put(WorkspaceObservationFrameDTO(
                subscription_id=source.subscription_id,
                state="error",
                status=status,
                detail_json=json.dumps(detail, ensure_ascii=False),
            ))
        else:
            await queue.put(WorkspaceObservationFrameDTO(
                subscription_id=source.subscription_id,
                state="closed",
            ))

    tasks = [asyncio.create_task(pump(source)) for source in sources]

    async def cleanup() -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    remaining = len(tasks)
    try:
        while remaining:
            frame = await queue.get()
            if frame.state in {"error", "closed"}:
                remaining -= 1
            yield frame
    finally:
        await run_cleanup_shielded(cleanup)
