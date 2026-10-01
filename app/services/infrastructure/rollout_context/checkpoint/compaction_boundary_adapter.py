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

from app.services.infrastructure.rollout_context.checkpoint.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
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

    由 RolloutStorage 组合；唯一配对事实来源仍是 tool_calls 投影与
    validate_tool_protocol_closure，本模块不维护第二套配对判断。
    """

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        """把 state 消息切点映射到 active view 前缀并逐个验证闭包。

        返回安全切点集合；映射损坏（已持久化消息脱离 active view 或
        持久化前缀与 view 顺序不一致）直接报错，不猜测修复。
        """
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("compaction preflight session_id 必须是非空字符串")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("compaction preflight checkpoint_ns 必须是字符串")
        candidates = list(cutoff_indexes)
        for cutoff in candidates:
            if type(cutoff) is not int:
                raise TypeError(
                    f"compaction preflight 切点必须是 int，实际值: {cutoff!r}"
                )
        with self._lock(session_id, checkpoint_ns):
            self.initialize(session_id, checkpoint_ns)
            with self._connect(session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                head = self._checkpoint_row(connection, checkpoint_ns, None)
                if head is None:
                    raise RuntimeError(
                        "compaction preflight 缺少 durable owner: "
                        f"session={session_id} checkpoint_ns={checkpoint_ns!r} "
                        "没有 active checkpoint"
                    )
                view_id = strict_text(head[6], field="checkpoints.view_id")
                view_sequences = self._view_message_sequences(
                    session_id,
                    checkpoint_ns,
                    view_id,
                    set(),
                    connection=connection,
                )
                view_positions = {
                    sequence: index for index, sequence in enumerate(view_sequences)
                }
                codec = self._codec()
                state_sequences: list[int | None] = []
                for index, message in enumerate(state_messages):
                    message_id = codec.message_id(message, index)
                    row = connection.execute(
                        "SELECT message_sequence FROM messages WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    if row is None:
                        # in-flight 未持久化消息只允许出现在已持久化前缀之后。
                        state_sequences.append(None)
                        continue
                    sequence = strict_non_negative_int(
                        row[0], field="messages.message_sequence"
                    )
                    if sequence not in view_positions:
                        raise RuntimeError(
                            "compaction preflight 映射损坏: state 消息已持久化"
                            f"但不在 active view 内 index={index} "
                            f"message_id={message_id} message_sequence={sequence}"
                        )
                    state_sequences.append(sequence)
                persisted = list(
                    dict.fromkeys(
                        sequence
                        for sequence in state_sequences
                        if sequence is not None
                    )
                )
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
                    durable_cutoff = sum(
                        1
                        for sequence in state_sequences[:cutoff]
                        if sequence is not None
                    )
                    try:
                        validate_tool_protocol_closure(
                            connection,
                            view_sequences,
                            durable_cutoff,
                        )
                    except ToolProtocolBoundaryConflict:
                        continue
                    safe.add(cutoff)
                return frozenset(safe)
