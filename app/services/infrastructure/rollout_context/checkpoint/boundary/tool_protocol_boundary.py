"""tool protocol 边界闭合 validator（唯一公共实现）。

rewind/history_replay/compaction 等 durable context boundary 都经由
create_context_boundary 计算目标 view 的可见 message 前缀。本模块在该
前缀上复用 canonical tool_calls 配对索引验证 closure：assistant
tool-call group 与各自匹配的 terminal tool result 必须整体位于边界同侧。
不解析消息正文、不猜测物理 offset、不合成 result、不借 source 闭合。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import parse_item_projection_manifest

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


@dataclass(frozen=True, slots=True)
class _ToolCarrier:
    message_id: str
    turn_id: str | None
    execution_id: str
    role: str
    status: str
    tool_call_id: str | None
    identity: ItemProjectionIdentity | None

    @property
    def checkpoint(self) -> bool:
        return self.identity is not None and self.identity.projection_message_id is not None

    @property
    def bound_message(self) -> bool:
        identity = self.identity
        return identity is None or identity.projection_message_id in {None, self.message_id}


def _tool_carriers(
    connection: sqlite3.Connection, sequences: Sequence[int],
) -> dict[int, _ToolCarrier]:
    carriers: dict[int, _ToolCarrier] = {}
    for offset in range(0, len(sequences), _QUERY_CHUNK_SIZE):
        chunk = sequences[offset : offset + _QUERY_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT message.message_sequence, message.message_id, message.turn_id, "
            "message.execution_id, message.role, item.status, item.tool_call_id, "
            "item.projection_manifest_json FROM messages AS message JOIN item_catalog AS item "
            "ON item.jsonl_offset = message.jsonl_offset AND item.jsonl_length = message.jsonl_length "
            f"WHERE message.message_sequence IN ({placeholders}) "
            "AND (message.role = 'tool' OR EXISTS (SELECT 1 FROM tool_calls AS call "
            "WHERE call.assistant_message_sequence = message.message_sequence))",
            tuple(chunk),
        ).fetchall()
        for sequence, message_id, turn_id, execution_id, role, status, call_id, manifest in rows:
            projection = parse_item_projection_manifest(json.loads(manifest))["projection_identity"]
            carriers[sequence] = _ToolCarrier(
                message_id, turn_id, execution_id, role, status, call_id,
                ItemProjectionIdentity.from_dict(projection) if projection is not None else None,
            )
    return carriers


def _same_tool_pair(call: _ToolCarrier, result: _ToolCarrier, call_id: str) -> bool:
    return (
        call.role == "assistant" and result.role == "tool"
        and result.status == "completed" and result.tool_call_id is not None
        and call.bound_message and result.bound_message
        and call.turn_id == result.turn_id and call.execution_id == result.execution_id
        and (call.identity.model_call_id if call.identity is not None else None)
        == (result.identity.model_call_id if result.identity is not None else None)
        and provider_tool_call_id(call.identity, call_id)
        == provider_tool_call_id(result.identity, result.tool_call_id)
    )


def _unsafe_cutoff_indices(
    connection: sqlite3.Connection,
    ordered_sequences: Sequence[int],
    position: dict[int, int],
) -> set[int]:
    """从配对索引计算所有会拆散 call/result 的前缀切点。

    配对仍只来自 tool_calls；canonical locator 证明 carrier 身份并捕获
    没有配对行的结果。隐藏的跨 sink 镜像只有在实际 carrier 的唯一声明
    已选中时才可忽略，不向 view 补回任何声明。
    """
    unsafe: set[int] = set()
    total = len(ordered_sequences)
    rows: set[tuple[str, int, int | None]] = set()
    for offset in range(0, total, _QUERY_CHUNK_SIZE):
        chunk = ordered_sequences[offset : offset + _QUERY_CHUNK_SIZE]
        placeholders = ",".join("(?)" for _ in chunk)
        rows.update(
            connection.execute(
                f"WITH selected(sequence) AS (VALUES {placeholders}) "
                "SELECT tool_call_id, assistant_message_sequence, result_message_sequence "
                "FROM tool_calls WHERE assistant_message_sequence IN (SELECT sequence FROM selected) "
                "OR result_message_sequence IN (SELECT sequence FROM selected)",
                tuple(chunk),
            ).fetchall()
        )
    carrier_sequences = set(ordered_sequences)
    for _call_id, assistant_sequence, result_sequence in rows:
        carrier_sequences.add(assistant_sequence)
        if result_sequence is not None:
            carrier_sequences.add(result_sequence)
    carriers = _tool_carriers(connection, sorted(carrier_sequences))
    unique_assistant_sequence_by_result: dict[int, int | None] = {}
    visible_results: set[int] = set()
    for call_id, assistant_sequence, result_sequence in rows:
        if result_sequence is None:
            continue
        call = carriers.get(assistant_sequence)
        result = carriers.get(result_sequence)
        if call is None or result is None or not _same_tool_pair(call, result, call_id):
            continue
        if call.checkpoint == result.checkpoint:
            # None 表示同一 result 已对应多条 assistant call。
            if result_sequence in unique_assistant_sequence_by_result:
                unique_assistant_sequence_by_result[result_sequence] = None
            else:
                unique_assistant_sequence_by_result[result_sequence] = assistant_sequence
        if assistant_sequence in position:
            visible_results.add(result_sequence)

    for call_id, assistant_sequence, result_sequence in rows:
        call = carriers.get(assistant_sequence)
        result = carriers.get(result_sequence) if result_sequence is not None else None
        paired = call is not None and result is not None and _same_tool_pair(call, result, call_id)
        if assistant_sequence not in position:
            unique_assistant_sequence = unique_assistant_sequence_by_result.get(
                result_sequence
            )
            if (
                paired and call is not None and result is not None
                and call.checkpoint != result.checkpoint
                and result.identity is not None and result.identity.model_call_id is not None
                and unique_assistant_sequence is not None
                and unique_assistant_sequence in position
            ):
                # 首个 terminal 可闭合两个待完成 sink；只按已选中的真实
                # carrier 配对检查，不能让隐藏镜像制造第二个未闭合工具组。
                continue
            # 可见 result 的声明被压缩或截去时，不能借 backing 补回可见 call。
            unsafe.update(range(position[result_sequence] + 1, total + 1))
            continue
        call_index = position[assistant_sequence]
        if result_sequence is None or result_sequence not in position:
            # result 尚未提交或不在 source view 内：包含 call 的任何更长
            # 前缀都未闭合。
            unsafe.update(range(call_index + 1, total + 1))
            continue
        result_index = position[result_sequence]
        if not paired or result_index <= call_index:
            # 身份错配或 result 早于 call 都属于损坏的配对投影。
            unsafe.update(range(min(call_index, result_index) + 1, total + 1))
            continue
        # cutoff ∈ (call_index, result_index] 时 call 已入、result 未入。
        unsafe.update(range(call_index + 1, result_index + 1))
    for sequence in ordered_sequences:
        carrier = carriers.get(sequence)
        if carrier is not None and carrier.role == "tool" and sequence not in visible_results:
            # 完全没有 declaration 行的真实 result 同样是 orphan，不能因
            # 配对查询零行而被误判为闭合。
            unsafe.update(range(position[sequence] + 1, total + 1))
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
        f"{CONFLICT_CODE}: 目标前缀未包含完整且有序的 assistant tool-call group 与匹配 "
        f"terminal result: cutoff_index={cutoff_index}, "
        f"source_view_size={len(ordered)}",
        safe_anchors=safe_anchors,
    )


__all__ = [
    "CONFLICT_CODE",
    "ToolProtocolBoundaryConflict",
    "ToolProtocolSafeAnchor",
    "validate_tool_protocol_closure",
]
