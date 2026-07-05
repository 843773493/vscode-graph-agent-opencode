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

    async def save_summarization_event(
        self,
        *,
        checkpoint: ContextCompactionCheckpoint,
        cutoff_index: int,
        summary_message: object,
        history_file_path: str,
    ) -> None:
        channel_values = dict(checkpoint._channel_values)
        channel_values["_summarization_event"] = {
            "cutoff_index": cutoff_index,
            "summary_message": summary_message,
            "file_path": history_file_path,
        }

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
        event_version = self._checkpointer.get_next_version(
            channel_versions.get("_summarization_event"),
            None,
        )
        channel_versions["_summarization_event"] = event_version
        next_checkpoint["channel_versions"] = channel_versions

        await self._checkpointer.aput(
            config=checkpoint._tuple.config,
            checkpoint=next_checkpoint,
            metadata={"source": "manual_compact", "step": -1, "writes": {}},
            new_versions={"_summarization_event": event_version},
        )
