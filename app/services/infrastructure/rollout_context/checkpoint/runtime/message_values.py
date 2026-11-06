"""在同一父快照中准备 messages 基准、冻结选集和当前增量。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    replay_message_values,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)

if TYPE_CHECKING:
    from app.domain.itemized.mutation_intents import MutationIntentOwner
    from app.services.infrastructure.rollout_context.checkpoint.durable.store import (
        RolloutCheckpointStore,
    )
    from app.services.infrastructure.rollout_context.checkpoint.reader import (
        RolloutContextReader,
    )

MessagesState = Literal["view", "delta", "snapshot"]


def prepare_message_values(
    reader: RolloutContextReader, checkpoints: RolloutCheckpointStore, *,
    owner: MutationIntentOwner, checkpoint_ns: str, parent_checkpoint_id: str | None,
    values: Mapping[str, object],
    versions: Mapping[str, object], new_versions: Mapping[str, object],
    metadata: Mapping[str, object],
) -> tuple[list[BaseMessage] | None, MessagesState, dict[str, str], str | None]:
    snapshot = None
    if isinstance(parent_checkpoint_id, str) and parent_checkpoint_id:
        try:
            snapshot = reader.open_snapshot(owner.session_id, owner.thread_id, checkpoint_ns)
        except RolloutNotMaterializedError:
            pass
    with snapshot if snapshot is not None else nullcontext():
        index = reader.latest_checkpoint(snapshot, parent_checkpoint_id) if snapshot is not None else None
        parent_versions: dict[str, str] = {}
        parent_delta = False
        if index is not None:
            parent_versions, _, parent_state = checkpoints.checkpoint_versions(snapshot, index.checkpoint_id)
            parent_delta = parent_state == "snapshot" or (
                parent_state == "delta" and "messages" in parent_versions
            )
        elif isinstance(parent_checkpoint_id, str) and "messages" in values:
            # 首个完整基准可独立建立根；省略值不能掩盖已引用父链的丢失。
            parent_checkpoint_id = None
        elif parent_checkpoint_id:
            raise RuntimeError(f"父 checkpoint 不可读取: {parent_checkpoint_id}")

        candidate = values.get("messages")
        counters = metadata.get("counters_since_delta_snapshot", {})
        if not isinstance(counters, Mapping):
            raise TypeError("checkpoint Delta counters 必须是 mapping")
        delta = isinstance(candidate, _DeltaSnapshot) or "messages" in counters or parent_delta
        state: MessagesState = "snapshot" if delta and "messages" in values else "delta" if delta else "view"
        read_parent = "messages" not in values and (
            "_summarization_event" in values or (delta and "messages" in new_versions)
        )
        parent_values = checkpoints.checkpoint_values(
            owner.session_id, owner.thread_id, checkpoint_ns, index,
            snapshot=snapshot, requested_channels=frozenset({"messages"}) if read_parent else frozenset(),
        ) if index is not None else {}
        if "messages" in values:
            current = candidate.value if isinstance(candidate, _DeltaSnapshot) else candidate
        elif index is not None:
            if not read_parent:
                # None 继承冻结 selection；未消费正文时不顺便恢复和校验旧消息。
                return None, state, parent_versions, parent_checkpoint_id
            # 每个 checkpoint 已冻结完整 messages 选集；普通写入直接消费该选集，
            # 不再经 get_tuple 解码无关通道并从祖先重放。公开恢复仍校验 Delta 依赖。
            current = parent_values["messages"]
            if delta and "messages" in new_versions:
                writes = checkpoints.pending_writes(
                    owner.session_id, owner.thread_id, checkpoint_ns, index.checkpoint_id,
                    snapshot=snapshot, requested_channels=frozenset({"messages"}),
                )
                current = replay_message_values(current, [value for _, _, value in writes])
        else:
            if delta and "messages" in versions:
                raise RuntimeError("messages Delta 缺少已引用的父 checkpoint 或基准")
            current = []
    if not isinstance(current, list) or not all(isinstance(message, BaseMessage) for message in current):
        raise TypeError("checkpoint messages 必须是 LangChain BaseMessage 列表")
    return current, state, parent_versions, parent_checkpoint_id
