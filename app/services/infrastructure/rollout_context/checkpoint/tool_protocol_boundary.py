"""tool protocol 边界闭合 validator（唯一公共实现）。

rewind/history_replay/compaction 等 durable context boundary 都经由
create_context_boundary 计算目标 view 的可见 message 前缀。本模块在该
前缀上复用 canonical tool_calls 配对索引验证 closure：assistant
tool-call group 与各自匹配的 terminal tool result 必须整体位于边界同侧。
不解析消息正文、不猜测物理 offset、不合成 result、不借 source 闭合。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

CONFLICT_CODE = "tool-protocol-boundary-conflict"

_QUERY_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class ToolProtocolSafeAnchor:
    """不含正文的安全 anchor 候选。

    cutoff_index 是 source view 有序 message sequence 上的前缀切点；
    cutoff_message_sequence 是该前缀内最后一条可见 message 的序号。
    """

    cutoff_index: int
    cutoff_message_sequence: int | None


class ToolProtocolBoundaryConflict(RuntimeError):
    """boundary 切入 assistant tool-call group 与 terminal result 之间。

    携带闭合错误码与可重选的安全 anchor 候选；不携带任何消息正文。
    """

    def __init__(
        self,
        message: str,
        *,
        safe_anchors: tuple[ToolProtocolSafeAnchor, ...],
    ) -> None:
        super().__init__(message)
        self.code = CONFLICT_CODE
        self.safe_anchors = safe_anchors


def _unsafe_cutoff_indices(
    connection: sqlite3.Connection,
    ordered_sequences: Sequence[int],
    position: dict[int, int],
) -> set[int]:
    """从配对索引计算所有会拆散 call/result 的前缀切点。

    tool_calls 表是 message projection 写入的唯一配对事实来源；
    status pending（result_message_sequence 为 NULL）与 result 位于
    view 之外都按未闭合处理，fail closed。
    """
    unsafe: set[int] = set()
    total = len(ordered_sequences)
    rows: list[tuple[object, ...]] = []
    for offset in range(0, total, _QUERY_CHUNK_SIZE):
        chunk = ordered_sequences[offset : offset + _QUERY_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            connection.execute(
                "SELECT assistant_message_sequence, result_message_sequence "
                f"FROM tool_calls WHERE assistant_message_sequence IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
        )
    for assistant_sequence, result_sequence in rows:
        if assistant_sequence not in position:
            continue
        call_index = position[assistant_sequence]
        if result_sequence is None or result_sequence not in position:
            # result 尚未提交或不在 source view 内：包含 call 的任何更长
            # 前缀都未闭合。
            unsafe.update(range(call_index + 1, total + 1))
            continue
        result_index = position[result_sequence]
        if result_index <= call_index:
            # result 序号早于 call 属于损坏的配对投影；按未闭合 fail closed。
            unsafe.update(range(call_index + 1, total + 1))
            continue
        # cutoff ∈ (call_index, result_index] 时 call 已入、result 未入。
        unsafe.update(range(call_index + 1, result_index + 1))
    return unsafe


def _safe_anchor(
    ordered_sequences: Sequence[int],
    cutoff_index: int,
) -> ToolProtocolSafeAnchor:
    last_sequence = ordered_sequences[cutoff_index - 1] if cutoff_index else None
    return ToolProtocolSafeAnchor(
        cutoff_index=cutoff_index,
        cutoff_message_sequence=last_sequence,
    )


def validate_tool_protocol_closure(
    connection: sqlite3.Connection,
    source_sequences: Sequence[int],
    cutoff_index: int,
) -> None:
    """验证 source_sequences[:cutoff_index] 不拆散 tool-call 配对。

    调用方必须在创建目标 view/branch/pending transition 之前调用本函数，
    保证冲突时旧 active view 与全局状态零副作用。
    """
    if type(cutoff_index) is not int:
        raise TypeError("tool protocol boundary cutoff_index 必须是 int")
    ordered = list(dict.fromkeys(source_sequences))
    if cutoff_index < 0 or cutoff_index > len(ordered):
        raise ValueError(
            "tool protocol boundary cutoff_index 超出 source view 范围: "
            f"cutoff_index={cutoff_index}, view_size={len(ordered)}"
        )
    position = {sequence: index for index, sequence in enumerate(ordered)}
    unsafe = _unsafe_cutoff_indices(connection, ordered, position)
    if cutoff_index not in unsafe:
        return
    below = max(
        (index for index in range(cutoff_index) if index not in unsafe),
        default=None,
    )
    above = min(
        (
            index
            for index in range(cutoff_index + 1, len(ordered) + 1)
            if index not in unsafe
        ),
        default=None,
    )
    safe_anchors = tuple(
        _safe_anchor(ordered, index)
        for index in (below, above)
        if index is not None
    )
    raise ToolProtocolBoundaryConflict(
        f"{CONFLICT_CODE}: 目标边界切入 assistant tool-call group 与匹配 "
        f"terminal result 之间: cutoff_index={cutoff_index}, "
        f"source_view_size={len(ordered)}",
        safe_anchors=safe_anchors,
    )


__all__ = [
    "CONFLICT_CODE",
    "ToolProtocolBoundaryConflict",
    "ToolProtocolSafeAnchor",
    "validate_tool_protocol_closure",
]
