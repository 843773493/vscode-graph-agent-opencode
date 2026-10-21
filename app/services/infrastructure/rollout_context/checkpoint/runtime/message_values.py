"""转换官方 messages checkpoint 的基准/省略值，复用同一消息恢复链。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
    replay_message_values,
)

MessagesState = Literal["view", "delta", "snapshot"]


def prepare_message_values(
    saver: BaseCheckpointSaver, *, values: Mapping[str, object],
    versions: Mapping[str, object], new_versions: Mapping[str, object],
    metadata: Mapping[str, object], parent: CheckpointTuple | None,
) -> tuple[list[BaseMessage], MessagesState]:
    candidate = values.get("messages")
    counters = metadata.get("counters_since_delta_snapshot", {})
    if not isinstance(counters, Mapping):
        raise TypeError("checkpoint Delta counters 必须是 mapping")
    parent_values = parent.checkpoint["channel_values"] if parent is not None else {}
    parent_delta = parent is not None and (
        isinstance(parent_values.get("messages"), _DeltaSnapshot)
        or ("messages" not in parent_values and "messages" in parent.checkpoint["channel_versions"])
    )
    delta = isinstance(candidate, _DeltaSnapshot) or "messages" in counters or parent_delta
    state: MessagesState = "view"
    if "messages" in values:
        current = candidate.value if isinstance(candidate, _DeltaSnapshot) else candidate
        if delta:
            state = "snapshot"
    elif delta:
        state = "delta"
        if parent is None:
            if "messages" in versions:
                raise RuntimeError("messages Delta 缺少已引用的父 checkpoint 或基准")
            current = []
        else:
            current = checkpoint_messages(saver, parent)
            if "messages" in new_versions:
                current = replay_message_values(current, [
                    value for _, channel, value in parent.pending_writes if channel == "messages"
                ])
    else:
        current = checkpoint_messages(saver, parent) if parent is not None else []
    if not isinstance(current, list) or not all(isinstance(message, BaseMessage) for message in current):
        raise TypeError("checkpoint messages 必须是 LangChain BaseMessage 列表")
    return current, state
