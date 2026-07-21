from __future__ import annotations

import uuid
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver, Checkpoint, CheckpointTuple

from app.core.checkpoint_config import build_checkpoint_config


@dataclass(slots=True)
class ContextCompactionCheckpoint:
    raw_messages: list[object]
    event: object
    _tuple: CheckpointTuple
    _checkpoint: Checkpoint
    _channel_values: dict[str, object]


class ContextCompactionCheckpointStore:
    def __init__(self, *, checkpointer: BaseCheckpointSaver) -> None:
        self._checkpointer = checkpointer

    async def load(self, session_id: str) -> ContextCompactionCheckpoint | None:
        tup = await self._checkpointer.aget_tuple(build_checkpoint_config(session_id))
        if tup is None:
            return None

        checkpoint = tup.checkpoint.copy()
        raw_channel_values = checkpoint.get("channel_values", {})
        if not isinstance(raw_channel_values, dict):
            raise TypeError(
                "LangGraph checkpoint channel_values 应为 dict，"
                f"实际类型: {type(raw_channel_values).__name__}"
            )
        channel_values: dict[str, object] = dict(raw_channel_values)
        raw_messages = channel_values.get("messages", [])
        if not isinstance(raw_messages, list):
            raise TypeError(
                f"LangGraph checkpoint messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        return ContextCompactionCheckpoint(
            raw_messages=raw_messages,
            event=channel_values.get("_summarization_event"),
            _tuple=tup,
            _checkpoint=checkpoint,
            _channel_values=channel_values,
        )

    async def save_compaction_request(
        self,
        *,
        checkpoint: ContextCompactionCheckpoint,
    ) -> None:
        await self._save_channel_value(
            checkpoint=checkpoint,
            channel="_force_cache_compaction",
            value=True,
            source="manual_compact_request",
        )

    async def _save_channel_value(
        self,
        *,
        checkpoint: ContextCompactionCheckpoint,
        channel: str,
        value: object,
        source: str,
    ) -> None:
        channel_values = dict(checkpoint._channel_values)
        channel_values[channel] = value

        next_checkpoint = checkpoint._checkpoint.copy()
        next_checkpoint["channel_values"] = channel_values
        next_checkpoint["id"] = str(uuid.uuid4())

        raw_channel_versions = next_checkpoint.get("channel_versions", {})
        if not isinstance(raw_channel_versions, dict):
            raise TypeError(
                "LangGraph checkpoint channel_versions 应为 dict，"
                f"实际类型: {type(raw_channel_versions).__name__}"
            )
        channel_versions = dict(raw_channel_versions)
        value_version = self._checkpointer.get_next_version(
            channel_versions.get(channel),
            None,
        )
        channel_versions[channel] = value_version
        next_checkpoint["channel_versions"] = channel_versions

        await self._checkpointer.aput(
            config=checkpoint._tuple.config,
            checkpoint=next_checkpoint,
            metadata={"source": source, "step": -1, "writes": {}},
            new_versions={channel: value_version},
        )
