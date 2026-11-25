from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
)


@dataclass(slots=True)
class ManualCompactionSnapshot:
    """手动压缩读取的 LangGraph 检查点快照。"""

    owner: MutationIntentOwner
    raw_messages: list[object]


class ManualCompactionCheckpointAdapter:
    """只从既有 checkpointer 读取手动压缩快照。"""

    def __init__(self, *, checkpointer: BaseCheckpointSaver) -> None:
        self._checkpointer = checkpointer

    async def load_snapshot(self, owner: MutationIntentOwner) -> ManualCompactionSnapshot | None:
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            source_tuple = await self._checkpointer.aget_tuple(build_checkpoint_config(owner.thread_id))
        if source_tuple is None:
            return None

        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            raw_messages = await acheckpoint_messages(self._checkpointer, source_tuple)
        if not isinstance(raw_messages, list):
            raise TypeError(
                f"LangGraph checkpoint messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        return ManualCompactionSnapshot(
            owner=owner,
            raw_messages=raw_messages,
        )
