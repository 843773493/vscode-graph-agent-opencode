"""SQLite bounded item projection 的写入和读取。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

_VISIBLE_TEXT_LIMIT = 64 * 1024


def _preview_text(value: object) -> str:
    """从 canonical payload 生成 bounded projection 文本。

    这是 storage 自己的有界索引投影，不是 LangChain/provider 映射。完整
    payload 仍只由 JSONL 保存；这里不依赖 ``MessageCodec``，使一次性
    legacy import 在没有 checkpoint 适配器时也能原子建立 v2 item projection。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = value.get("text")
            return text if isinstance(text, str) else ""
        if block_type == "refusal":
            refusal = value.get("refusal")
            return f"[拒绝]{refusal}" if isinstance(refusal, str) else ""
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(_preview_text(item) for item in value)
    return ""


def _canonical_projection_content(item: CanonicalItemRecord) -> str:
    """返回 item projection 的 bounded-visible 文本，不构造消息对象。"""
    payload = item.payload
    if item.semantic_kind == SemanticKind.TOOL_CALL:
        raw_calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
        calls = raw_calls if isinstance(raw_calls, list) else [payload]
        return ", ".join(
            str(call.get("name"))
            for call in calls
            if isinstance(call, Mapping)
            and isinstance(call.get("name"), str)
            and call.get("name")
        )
    if item.semantic_kind == SemanticKind.TOOL_RESULT:
        value = payload.get("content") if isinstance(payload, Mapping) else None
        return _preview_text(value) or (str(value) if isinstance(value, str) else "")
    if item.semantic_kind == SemanticKind.COMPACTION_SUMMARY:
        value = payload.get("summary") if isinstance(payload, Mapping) else None
        return _preview_text(value) or (str(value) if isinstance(value, str) else "")
    if item.semantic_kind in {
        SemanticKind.USER_INPUT,
        SemanticKind.ASSISTANT_OUTPUT,
        SemanticKind.REASONING,
        SemanticKind.RUNTIME_NOTICE,
    }:
        return _preview_text(payload) or (
            str(payload) if isinstance(payload, str) else ""
        )
    return ""


def _projection_bool(value: object, *, field: str) -> int:
    if type(value) is not int or value not in {0, 1}:
        raise RuntimeError(f"{field} 必须是 SQLite boolean 0/1")
    return value


def _decode_item_projection_row(row: tuple[object, ...]) -> dict[str, object]:
    if len(row) != 20:
        raise RuntimeError("item_projections 字段数量不一致")
    (
        item_sequence,
        item_id,
        projection_id,
        semantic_kind,
        payload_kind,
        status,
        turn_id,
        turn_scope,
        message_group_id,
        wire_role,
        content,
        content_length,
        content_truncated,
        has_reasoning,
        has_tool_calls,
        phase,
        content_hash,
        projection_version,
        created_at,
        updated_at,
    ) = row
    item_sequence = strict_non_negative_int(
        item_sequence, field="item_projections.item_sequence"
    )
    if item_sequence == 0:
        raise RuntimeError("item_projections.item_sequence 必须大于 0")
    item_id = strict_text(item_id, field="item_projections.item_id")
    projection_id = strict_text(projection_id, field="item_projections.id")
    if projection_id != item_id:
        raise RuntimeError(f"item_projections.id 与 item_id 不一致: {item_id}")
    semantic_kind = strict_text(
        semantic_kind, field=f"item_projections.semantic_kind:{item_id}"
    )
    if semantic_kind not in {value.value for value in SemanticKind}:
        raise RuntimeError(f"item_projections.semantic_kind 未知: {item_id}")
    payload_kind = strict_text(
        payload_kind, field=f"item_projections.payload_kind:{item_id}"
    )
    if payload_kind not in {value.value for value in PayloadKind}:
        raise RuntimeError(f"item_projections.payload_kind 未知: {item_id}")
    status = strict_text(status, field=f"item_projections.status:{item_id}")
    if status not in {value.value for value in CanonicalItemStatus}:
        raise RuntimeError(f"item_projections.status 未知: {item_id}")
    turn_id = strict_optional_text(turn_id, field=f"item_projections.turn_id:{item_id}")
    turn_scope = strict_optional_text(
        turn_scope, field=f"item_projections.turn_scope:{item_id}"
    )
    if turn_scope is not None and turn_scope not in {
        value.value for value in TurnScope
    }:
        raise RuntimeError(f"item_projections.turn_scope 未知: {item_id}")
    if turn_id is not None and turn_scope is None:
        raise RuntimeError(f"item_projections.turn_id 缺少 turn_scope: {item_id}")
    message_group_id = strict_optional_text(
        message_group_id,
        field=f"item_projections.message_group_id:{item_id}",
    )
    wire_role = strict_optional_text(
        wire_role, field=f"item_projections.wire_role:{item_id}"
    )
    content = strict_text(
        content,
        field=f"item_projections.content:{item_id}",
        allow_empty=True,
    )
    content_length = strict_non_negative_int(
        content_length, field=f"item_projections.content_length:{item_id}"
    )
    content_truncated = _projection_bool(
        content_truncated, field=f"item_projections.content_truncated:{item_id}"
    )
    if (
        content_truncated
        and (len(content) != _VISIBLE_TEXT_LIMIT or content_length <= len(content))
    ) or (not content_truncated and content_length != len(content)):
        raise RuntimeError(f"item_projections.content_length/truncated 不一致: {item_id}")
    has_reasoning = _projection_bool(
        has_reasoning, field=f"item_projections.has_reasoning:{item_id}"
    )
    has_tool_calls = _projection_bool(
        has_tool_calls, field=f"item_projections.has_tool_calls:{item_id}"
    )
    phase = strict_optional_text(phase, field=f"item_projections.phase:{item_id}")
    content_hash = strict_text(
        content_hash, field=f"item_projections.content_hash:{item_id}"
    )
    projection_version = strict_non_negative_int(
        projection_version, field=f"item_projections.projection_version:{item_id}"
    )
    if projection_version == 0:
        raise RuntimeError(f"item_projections.projection_version 必须大于 0: {item_id}")
    created_at = strict_text(created_at, field=f"item_projections.created_at:{item_id}")
    updated_at = strict_text(updated_at, field=f"item_projections.updated_at:{item_id}")
    return {
        "item_sequence": item_sequence,
        "item_id": item_id,
        "id": projection_id,
        "semantic_kind": semantic_kind,
        "payload_kind": payload_kind,
        "status": status,
        "turn_id": turn_id,
        "turn_scope": turn_scope,
        "message_group_id": message_group_id,
        "wire_role": wire_role,
        "content": content,
        "content_length": content_length,
        "content_truncated": content_truncated,
        "has_reasoning": has_reasoning,
        "has_tool_calls": has_tool_calls,
        "phase": phase,
        "content_hash": content_hash,
        "projection_version": projection_version,
        "created_at": created_at,
        "updated_at": updated_at,
    }


class RolloutProjectionMixin:
    """只写入派生 projection，不创建第二份 canonical payload 事实源。"""


    def _insert_item_projection(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
        timestamp: str,
    ) -> None:
        """写入 bounded item projection；完整 payload 仍只在 JSONL。"""
        content = _canonical_projection_content(item)
        truncated = len(content) > _VISIBLE_TEXT_LIMIT
        phase = {
            SemanticKind.USER_INPUT: "user_input",
            SemanticKind.ASSISTANT_OUTPUT: "assistant_text",
            SemanticKind.REASONING: "reasoning",
            SemanticKind.TOOL_CALL: "tool_request",
            SemanticKind.TOOL_RESULT: "tool_result",
            SemanticKind.RUNTIME_NOTICE: "runtime_notice",
            SemanticKind.COMPACTION_SUMMARY: "summary",
        }.get(item.semantic_kind)
        result = connection.execute(
            "INSERT INTO item_projections(item_sequence, item_id, id, semantic_kind, payload_kind, status, turn_id, turn_scope, message_group_id, wire_role, content, content_length, content_truncated, has_reasoning, has_tool_calls, phase, content_hash, projection_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.item_sequence,
                item.item_id,
                item.item_id,
                item.semantic_kind,
                item.payload_kind,
                item.status,
                item.turn_id,
                item.turn_scope,
                item.message_group_id,
                item.wire_role,
                content[:_VISIBLE_TEXT_LIMIT],
                len(content),
                int(truncated),
                int(item.semantic_kind == SemanticKind.REASONING),
                int(item.semantic_kind == SemanticKind.TOOL_CALL),
                phase,
                item.content_hash,
                1,
                item.created_at,
                timestamp,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"item projection 写入影响行数异常: {item.item_id}")


    def read_item_projections(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        """读取 item 级轻量 projection，不打开 JSONL 正文。"""
        self.initialize(thread_id, checkpoint_ns, validate_jsonl_items=False)
        values = tuple(dict.fromkeys(item_ids or ()))
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            columns = "item_sequence, item_id, id, semantic_kind, payload_kind, status, turn_id, turn_scope, message_group_id, wire_role, content, content_length, content_truncated, has_reasoning, has_tool_calls, phase, content_hash, projection_version, created_at, updated_at"
            invalid = connection.execute(
                "SELECT ip.item_id FROM item_projections AS ip "
                "LEFT JOIN item_catalog AS ic ON ic.item_id = ip.item_id "
                "AND ic.item_sequence = ip.item_sequence "
                "AND ic.semantic_kind = ip.semantic_kind "
                "AND ic.payload_kind = ip.payload_kind "
                "AND ic.status = ip.status "
                "AND ic.turn_id IS ip.turn_id "
                "AND ic.turn_scope IS ip.turn_scope "
                "AND ic.message_group_id IS ip.message_group_id "
                "AND ic.wire_role IS ip.wire_role "
                "AND ic.content_hash = ip.content_hash "
                "AND ic.created_at = ip.created_at "
                "WHERE ic.item_id IS NULL LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise RuntimeError(
                    "item projection 与 canonical catalog identity/hash 不一致: "
                    f"{invalid[0]}"
                )
            if values:
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"SELECT {columns} FROM item_projections WHERE item_id IN ({placeholders}) ORDER BY item_sequence",
                    values,
                ).fetchall()
                found_ids = {
                    strict_text(row[1], field="item_projections.item_id")
                    for row in rows
                }
                missing_ids = tuple(
                    item_id for item_id in values if item_id not in found_ids
                )
                if missing_ids:
                    raise KeyError(
                        "item projection 缺少请求的 canonical item: "
                        + ",".join(missing_ids)
                    )
            else:
                rows = connection.execute(
                    f"SELECT {columns} FROM item_projections ORDER BY item_sequence"
                ).fetchall()
        return [_decode_item_projection_row(row) for row in rows]
