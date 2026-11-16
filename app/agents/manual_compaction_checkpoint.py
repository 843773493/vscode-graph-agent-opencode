from __future__ import annotations

import uuid
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver, Checkpoint, CheckpointTuple

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
    message_snapshot,
    message_snapshot_metadata,
)


@dataclass(slots=True)
class ManualCompactionSnapshot:
    """手动压缩预览与请求提交共用的 LangGraph 检查点快照。"""

    owner: MutationIntentOwner
    raw_messages: list[object]
    summarization_event: object
    source_tuple: CheckpointTuple
    graph_checkpoint: Checkpoint
    channel_values: dict[str, object]


class ManualCompactionCheckpointAdapter:
    """读取 LangGraph 快照，将手动压缩请求写回既有 checkpointer。"""

    def __init__(self, *, checkpointer: BaseCheckpointSaver) -> None:
        self._checkpointer = checkpointer

    async def load_snapshot(self, owner: MutationIntentOwner) -> ManualCompactionSnapshot | None:
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            source_tuple = await self._checkpointer.aget_tuple(build_checkpoint_config(owner.thread_id))
        if source_tuple is None:
            return None

        graph_checkpoint = source_tuple.checkpoint.copy()
        raw_channel_values = graph_checkpoint.get("channel_values", {})
        if not isinstance(raw_channel_values, dict):
            raise TypeError(
                "LangGraph checkpoint channel_values 应为 dict，"
                f"实际类型: {type(raw_channel_values).__name__}"
            )
        channel_values: dict[str, object] = dict(raw_channel_values)
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            raw_messages = await acheckpoint_messages(self._checkpointer, source_tuple)
        channel_values["messages"] = raw_messages
        if not isinstance(raw_messages, list):
            raise TypeError(
                f"LangGraph checkpoint messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        return ManualCompactionSnapshot(
            owner=owner,
            raw_messages=raw_messages,
            summarization_event=channel_values.get("_summarization_event"),
            source_tuple=source_tuple,
            graph_checkpoint=graph_checkpoint,
            channel_values=channel_values,
        )

    async def request_compaction(
        self,
        *,
        snapshot: ManualCompactionSnapshot,
    ) -> None:
        channel = "_force_cache_compaction"
        channel_values = dict(snapshot.channel_values)
        channel_values["messages"] = message_snapshot(snapshot.raw_messages)
        channel_values[channel] = True

        next_checkpoint = snapshot.graph_checkpoint.copy()
        next_checkpoint["channel_values"] = channel_values
        next_checkpoint["id"] = str(uuid.uuid4())

        raw_channel_versions = next_checkpoint.get("channel_versions", {})
        if not isinstance(raw_channel_versions, dict):
            raise TypeError(
                "LangGraph checkpoint channel_versions 应为 dict，"
                f"实际类型: {type(raw_channel_versions).__name__}"
            )
        channel_versions = dict(raw_channel_versions)
        request_version = self._checkpointer.get_next_version(
            channel_versions.get(channel),
            None,
        )
        channel_versions[channel] = request_version
        next_checkpoint["channel_versions"] = channel_versions

        with session_thread_owner_scope(snapshot.owner, langgraph_thread_id=snapshot.owner.thread_id):
            await self._checkpointer.aput(
                config=snapshot.source_tuple.config,
                checkpoint=next_checkpoint,
                metadata={**message_snapshot_metadata(snapshot.source_tuple.metadata), "source": "manual_compact_request", "step": -1, "writes": {}},
                new_versions={channel: request_version},
            )
