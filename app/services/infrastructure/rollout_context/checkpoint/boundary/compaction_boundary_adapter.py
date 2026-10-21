"""compaction 边界的 typed adapter：只委托唯一 tool protocol validator。

cache_preserving 压缩的全部边界候选（稳定前缀切点、state cutoff、
overflow retry 切点）都规范为 source view 上的前缀切点。本模块不解析
消息正文，不维护第二套配对判断，统一经
validate_tool_protocol_closure 验证；冲突时抛出唯一边界异常。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from langchain_core.messages import AIMessage, ToolMessage

from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    find_message_locator,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)


def validate_compaction_prefix_cutoffs(
    connection: sqlite3.Connection,
    ordered_sequences: Sequence[int],
    cutoff_indexes: Iterable[int],
) -> None:
    """逐个验证 compaction 候选前缀切点，冲突时抛出唯一边界异常。

    任一切点拆散 assistant tool-call group 与匹配 terminal result 即抛出
    ToolProtocolBoundaryConflict（code=tool-protocol-boundary-conflict）。
    本函数只读，不产生任何持久化副作用。
    """
    for cutoff_index in cutoff_indexes:
        validate_tool_protocol_closure(connection, ordered_sequences, cutoff_index)


__all__ = [
    "CompactionPreflightPort",
    "RolloutCompactionPreflightOwnerMixin",
    "prefix_has_open_tool_group",
    "validate_compaction_prefix_cutoffs",
]


class CompactionPreflightPort(Protocol):
    """Agent 层持有的 compaction preflight 只读端口。

    生产实现由 RolloutCheckpointSaver 提供；Agent 层只依赖本协议，
    不接触 SQLite connection，也不感知 storage 内部结构。
    """

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        """返回候选切点中不拆散 tool-call 配对的安全子集。"""
        ...


def prefix_has_open_tool_group(
    state_messages: Sequence[object],
    cutoff_index: int,
) -> bool:
    """检测 in-flight tool group：call 已入前缀但 terminal result 未入。

    未持久化的配对无法经 SQLite tool_calls 索引验证，按协议在内存侧
    fail closed；call 缺少可配对 id 同样视为未闭合。
    """
    pending_call_ids: set[str] = set()
    for message in state_messages[:cutoff_index]:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                call_id = (
                    tool_call.get("id") if isinstance(tool_call, Mapping) else None
                )
                if not isinstance(call_id, str) or not call_id:
                    return True
                pending_call_ids.add(call_id)
        elif isinstance(message, ToolMessage):
            pending_call_ids.discard(message.tool_call_id)
    return bool(pending_call_ids)


class RolloutCompactionPreflightOwnerMixin:
    """compaction preflight 只读 owner：state 切点 → durable view 前缀闭包。

    由 RolloutContextStore 组合；唯一配对事实来源仍是 tool_calls 投影与
    validate_tool_protocol_closure，本模块不维护第二套配对判断。
    """

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        """把 raw state 切点映射到冻结 messages view 并逐个验证闭包。

        state/event 保留原坐标；Provider carrier selection 的去重不能改变它。
        raw 前段可由已冻结 event 证明已压缩；其余消息仍须属于 active history。
        全部持久化消息必须按冻结 channel 的前缀排序。
        """
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("compaction preflight session_id 必须是非空字符串")
        if not isinstance(thread_id, str) or not thread_id:
            raise TypeError("compaction preflight thread_id 必须是非空字符串")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("compaction preflight checkpoint_ns 必须是字符串")
        candidates = list(cutoff_indexes)
        for cutoff in candidates:
            if type(cutoff) is not int:
                raise TypeError(
                    f"compaction preflight 切点必须是 int，实际值: {cutoff!r}"
                )
        with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
            connection = snapshot.connection
            head = self.latest_checkpoint(
                session_id, thread_id, checkpoint_ns, None, snapshot=snapshot,
            )
            if head is None:
                raise RuntimeError(
                    "compaction preflight 缺少 durable owner: "
                    f"session={session_id} checkpoint_ns={checkpoint_ns!r} "
                    "没有 active checkpoint"
                )
            values = self.checkpoint_values(
                session_id, thread_id, checkpoint_ns, head, snapshot=snapshot,
                include_execution_context=True,
            )
            # stream 与 checkpoint 的 typed carrier 可在结果落盘前切换；raw
            # state 始终引用冻结 checkpoint carrier，不能拿 Provider 去重后的
            # 消息 ID 当作 state 的坐标。复用真实冷恢复 reader，排除冻结
            # channel 内已被最终 carrier 替代的中间 checkpoint 消息。
            codec = self._storage._codec()
            frozen_messages = values["messages"]
            frozen_locators = [find_message_locator(
                connection, codec.message_id(message, index),
                item_id=canonical_message_anchor(codec.to_dict(message)),
            ) for index, message in enumerate(frozen_messages)]
            if any(locator is None for locator in frozen_locators):
                raise RuntimeError("compaction preflight 冻结消息缺少 canonical locator")
            view_sequences = [locator[0] for locator in frozen_locators]
            message_sequences = {
                codec.message_id(message, index): sequence
                for index, (message, sequence) in enumerate(zip(frozen_messages, view_sequences, strict=True))
            }
            active_sequences = {row[0] for row in connection.execute(
                "SELECT message.message_sequence FROM context_view_items AS member "
                "JOIN item_catalog AS item ON item.item_id = member.item_id "
                "JOIN messages AS message ON message.jsonl_offset = item.jsonl_offset "
                "AND message.jsonl_length = item.jsonl_length "
                "WHERE member.view_id = ? AND member.visible = 1",
                (self.active_view_id(snapshot),),
            )}
            view_positions = {
                sequence: index for index, sequence in enumerate(view_sequences)
            }
            previous_cutoff = 0
            prefix_ids: set[str] = set()
            event = values.get("_summarization_event")
            if event is not None:
                if not isinstance(event, Mapping):
                    raise TypeError("compaction preflight 已冻结 event 必须是 mapping")
                previous_cutoff = strict_non_negative_int(event.get("cutoff_index"), field="compaction cutoff_index")
                if previous_cutoff > len(frozen_messages):
                    raise RuntimeError("compaction preflight 已冻结 cutoff 超过 raw channel")
                prefixes = event.get("cache_prefix_messages", []) if event.get("strategy") == "cache_preserving" else []
                if not isinstance(prefixes, (list, tuple)) or len(prefixes) > previous_cutoff:
                    raise RuntimeError("compaction preflight 已冻结 prefix 超过 raw cutoff")
                prefix_ids = {codec.message_id(prefix, index) for index, prefix in enumerate(prefixes)}
                if any(message_sequences.get(message_id) not in active_sequences for message_id in prefix_ids):
                    raise RuntimeError("compaction preflight 已冻结 prefix 脱离 active history")
                cutoff_id = event.get("cutoff_message_id")
                if cutoff_id is not None and (previous_cutoff == 0 or
                    codec.message_id(frozen_messages[previous_cutoff - 1], previous_cutoff - 1) != cutoff_id):
                    raise RuntimeError("compaction preflight 已冻结 cutoff identity 与 raw channel 不一致")
                summary_id = codec.message_id(event.get("summary_message"), 0)
                if message_sequences.get(summary_id) not in active_sequences:
                    raise RuntimeError("compaction preflight 已冻结摘要脱离 active history")
            state_sequences: list[int | None] = []
            durable_cutoffs = [0]
            has_unpersisted = False
            for index, message in enumerate(state_messages):
                message_id = codec.message_id(message, index)
                row = find_message_locator(
                    connection, message_id, item_id=canonical_message_anchor(codec.to_dict(message)),
                    sequences=view_sequences,
                )
                if row is None:
                    row = find_message_locator(connection, message_id)
                if row is None:
                    # in-flight 未持久化消息只允许出现在已持久化前缀之后。
                    state_sequences.append(None)
                    durable_cutoffs.append(durable_cutoffs[-1])
                    has_unpersisted = True
                    continue
                sequence = strict_non_negative_int(
                    row[0], field="messages.message_sequence"
                )
                compressed_middle = index < previous_cutoff and message_id not in prefix_ids
                if sequence not in view_positions or (not compressed_middle and sequence not in active_sequences):
                    raise RuntimeError(
                        "compaction preflight 映射损坏: state 消息已持久化"
                        f"但不在 active view 内 index={index} "
                        f"message_id={message_id} message_sequence={sequence}"
                    )
                if has_unpersisted:
                    raise RuntimeError(
                        "compaction preflight 映射损坏: state 未持久化消息后"
                        f"再次出现已持久化消息 index={index} message_id={message_id}"
                    )
                state_sequences.append(sequence)
                durable_cutoffs.append(view_positions[sequence] + 1)
            persisted = [sequence for sequence in state_sequences if sequence is not None]
            if persisted != view_sequences[: len(persisted)]:
                raise RuntimeError(
                    "compaction preflight 映射损坏: state 持久化前缀与 "
                    "active view 前缀顺序不一致 "
                    f"persisted={persisted[:8]} view={view_sequences[:8]}"
                )
            safe: set[int] = set()
            for cutoff in candidates:
                if cutoff < 0 or cutoff > len(state_messages):
                    raise ValueError(
                        "compaction preflight 切点超出 state 消息范围: "
                        f"cutoff={cutoff}, state_messages={len(state_messages)}"
                    )
                if prefix_has_open_tool_group(state_messages, cutoff):
                    continue
                try:
                    validate_tool_protocol_closure(
                        connection,
                        view_sequences,
                        durable_cutoffs[cutoff],
                    )
                except ToolProtocolBoundaryConflict:
                    continue
                safe.add(cutoff)
            return frozenset(safe)
