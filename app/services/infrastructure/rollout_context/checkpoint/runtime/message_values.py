"""转换官方 messages checkpoint 的基准/省略值，复用同一消息恢复链。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
    replay_message_values,
)

MessagesState = Literal["view", "delta", "snapshot"]


def prepare_message_values(
    saver: BaseCheckpointSaver, *, values: Mapping[str, object],
    versions: Mapping[str, object], new_versions: Mapping[str, object],
    metadata: Mapping[str, object], parent_config: RunnableConfig | None, parent_delta: bool,
) -> tuple[list[BaseMessage] | None, MessagesState]:
    candidate = values.get("messages")
    counters = metadata.get("counters_since_delta_snapshot", {})
    if not isinstance(counters, Mapping):
        raise TypeError("checkpoint Delta counters 必须是 mapping")
    delta = isinstance(candidate, _DeltaSnapshot) or "messages" in counters or parent_delta
    state: MessagesState = "view"
    if "messages" in values:
        current = candidate.value if isinstance(candidate, _DeltaSnapshot) else candidate
        if delta:
            state = "snapshot"
    else:
        state = "delta" if delta else "view"
        if (
            parent_config is not None and (not delta or "messages" not in new_versions)
            and "_summarization_event" not in values
        ):
            # None 表示继承冻结 selection，绝不把省略值伪装成空消息。
            # Delta 仅在 messages 更新时消费父 pending writes；版本未变无需恢复旧正文。
            # 压缩仍消费正文，沿官方恢复链路取得完整输入。
            return None, state
        parent = saver.get_tuple(parent_config) if parent_config is not None else None
        if parent_config is not None and parent is None:
            raise RuntimeError("messages 恢复缺少已引用的父 checkpoint")
        if parent is None:
            if delta and "messages" in versions:
                raise RuntimeError("messages Delta 缺少已引用的父 checkpoint 或基准")
            current = []
        else:
            current = checkpoint_messages(saver, parent)
            if delta and "messages" in new_versions:
                current = replay_message_values(current, [
                    value for _, channel, value in parent.pending_writes if channel == "messages"
                ])
    if not isinstance(current, list) or not all(isinstance(message, BaseMessage) for message in current):
        raise TypeError("checkpoint messages 必须是 LangChain BaseMessage 列表")
    return current, state
