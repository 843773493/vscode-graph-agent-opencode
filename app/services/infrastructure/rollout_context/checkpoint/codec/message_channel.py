"""官方消息 Delta channel 的输入归一与 checkpoint 恢复合同。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    BaseMessageChunk,
    RemoveMessage,
    convert_to_messages,
    message_chunk_to_message,
)
from langgraph.channels import DeltaChannel
from langgraph.channels.binop import _get_overwrite
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, PendingWrite
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph.message import _messages_delta_reducer, add_messages
from langgraph.pregel._checkpoint import (
    achannels_from_checkpoint,
    channels_from_checkpoint,
)
from langgraph.pregel._messages import ensure_message_ids


def _messages(value: object, *, require_ids: bool = True) -> list[AnyMessage]:
    values = value if isinstance(value, list) else [value]
    result = [
        message_chunk_to_message(message) if isinstance(message, BaseMessageChunk) else message
        for message in convert_to_messages(values)
    ]
    if require_ids and any(not isinstance(message.id, str) or not message.id for message in result):
        raise ValueError("messages Delta 写入必须在持久化前取得稳定 ID")
    return result


def replay_message_values(
    state: list[AnyMessage], writes: Sequence[object], *, require_ids: bool = True,
) -> list[AnyMessage]:
    """补齐 add_messages 的控制输入合同，正文合并仍由官方 reducer 完成。

    上游 DeltaChannel 保留 exact reducer identity，ensure_message_ids 会在
    持久化 writes 前处理所有 message-like。这里只规范 chunk、清空与删除，
    不生成恢复时变化的 UUID，也不另写消息合并算法。
    """
    base = state
    start = 0
    for index, value in enumerate(writes):
        overwrite, content = _get_overwrite(value)
        if overwrite:
            base = [] if content is None else _messages(content, require_ids=require_ids)
            start = index + 1
    batches: list[list[AnyMessage]] = []
    for value in writes[start:]:
        batch = _messages(value, require_ids=require_ids)
        if any(isinstance(message, RemoveMessage) for message in batch):
            # 删除再添加同 ID 的位置、重复删除与清空边界由官方 add_messages 决定。
            folded = _messages_delta_reducer(base, batches)
            unidentified = [message for message in [*folded, *batch] if message.id is None]
            base = add_messages(folded, batch)
            # fresh read 的原生输入尚未由 put_writes 规范；不能暴露临时转换对象的 UUID。
            for message in unidentified:
                message.id = None
            batches = []
        else:
            batches.append(batch)
    return _messages_delta_reducer(base, batches)


class MessagesDeltaChannel(DeltaChannel[list[AnyMessage]]):
    """使用官方基准、计数和重放协议，保留产品既有消息输入语义。"""

    def __init__(
        self, reducer: Callable = _messages_delta_reducer, typ: type = list,
        *, snapshot_frequency: int = 1000,
    ) -> None:
        if reducer is not _messages_delta_reducer:
            raise ValueError("messages Delta 必须使用官方消息 reducer")
        super().__init__(reducer, typ, snapshot_frequency=snapshot_frequency)

    def update(self, values: Sequence[object]) -> bool:
        if not values:
            return False
        # 条件边的 fresh read 先于 put_writes；在此规范原消息对象，后续持久化复用同一 ID。
        # 冷重放只经过 replay_writes，仍拒绝无 ID 的已存写入。
        writes = [
            ("messages", message)
            for value in values
            for overwrite, replacement in [_get_overwrite(value)]
            for content in [replacement if overwrite else value]
            for message in (content if isinstance(content, (list, tuple)) else [content])
            if isinstance(message, BaseMessage)
        ]
        # 原生 dict/tuple 的转换由 put_writes 持有的写列表完成；预览不生成无法回写的 UUID。
        ensure_message_ids({"messages": self}, writes)
        overwrites = [value for value in values if _get_overwrite(value)[0]]
        if overwrites:
            # 同一 superstep 多次 Overwrite 仍由官方实现拒绝。
            changed = super().update(values)
            self.value = [] if self.value is None else _messages(self.value, require_ids=False)
            return changed
        self.value = replay_message_values(self.get() if self.is_available() else [], values, require_ids=False)
        return True

    def replay_writes(self, writes: Sequence[PendingWrite]) -> None:
        self.value = replay_message_values(self.get(), [value for _, _, value in writes])


def checkpoint_messages(saver: BaseCheckpointSaver, checkpoint: CheckpointTuple) -> list[AnyMessage]:
    """业务读取和官方图共用基准/增量恢复，不能将省略值误读为空列表。"""
    channels, _ = channels_from_checkpoint(
        {"messages": MessagesDeltaChannel()}, checkpoint.checkpoint,
        saver=saver, config=checkpoint.config,
    )
    return channels["messages"].get()


async def acheckpoint_messages(saver: BaseCheckpointSaver, checkpoint: CheckpointTuple) -> list[AnyMessage]:
    channels, _ = await achannels_from_checkpoint(
        {"messages": MessagesDeltaChannel()}, checkpoint.checkpoint,
        saver=saver, config=checkpoint.config,
    )
    return channels["messages"].get()


def message_snapshot_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """手动完整基准重置 messages 计数，保留其它 Delta 通道的恢复期限。"""
    counters = metadata.get("counters_since_delta_snapshot", {})
    if not isinstance(counters, Mapping):
        raise TypeError("checkpoint Delta counters 必须是 mapping")
    return {**metadata, "counters_since_delta_snapshot": {
        channel: value for channel, value in counters.items() if channel != "messages"
    }}


def message_snapshot(messages: list[AnyMessage]) -> _DeltaSnapshot:
    """隔离框架基准封装，手动写入方仍只处理完整消息列表。"""
    return _DeltaSnapshot(messages)
