"""LangGraph CheckpointSaver 的异步适配层。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutTurnAnchor,
)


class RolloutLangGraphAsyncMixin:
    """仅把同步 Saver port 放入线程，不复制 checkpoint 业务规则。"""

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)

    async def arewind(
        self,
        config: RunnableConfig,
        *,
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.rewind,
            config,
            checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )

    async def aresolve_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        return await asyncio.to_thread(
            self.resolve_turn_anchor,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
            require_completed=require_completed,
        )

    async def aresolve_latest_completed_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        return await asyncio.to_thread(
            self.resolve_latest_completed_turn_anchor,
            config,
            anchor_mode=anchor_mode,
        )

    async def amaterialize_turn_anchor(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> tuple[RolloutTurnAnchor, list[BaseMessage]]:
        return await asyncio.to_thread(
            self.materialize_turn_anchor,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    async def arewind_to_turn(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.rewind_to_turn,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    async def ahistory_replay(
        self,
        config: RunnableConfig,
        *,
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.history_replay,
            config,
            turn_id=turn_id,
            anchor_mode=anchor_mode,
        )


__all__ = ["RolloutLangGraphAsyncMixin"]
