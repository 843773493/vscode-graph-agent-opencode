"""LangChain message projection 的 checkpoint owner。

canonical item 是 JSONL/catalog 的唯一正文事实；本模块只把已提交 item
映射成 history/message locator 和 bounded tool/reasoning 索引。MessageCodec
由 checkpoint 组装层注入，本模块不自行实现 LangChain 纯映射。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

_VISIBLE_TEXT_LIMIT = 64 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class RolloutMessageProjectionMixin:
    """维护 message、tool 和 reasoning 的派生 projection。"""

    def _materialize_canonical_message_projection(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
        *,
        thread_id: str,
        checkpoint_ns: str,
        commit_id: int,
        message_sequence: int,
        timestamp: str,
    ) -> tuple[int, str, str] | None:
        """把可映射的 canonical item 镜像成同一事务内的 message projection。"""
        message_sequence = strict_non_negative_int(
            message_sequence, field=f"messages.message_sequence: {item.item_id}"
        )
        if message_sequence == 0:
            raise ValueError(f"message_sequence 必须为正数: {item.item_id}")
        commit_id = strict_non_negative_int(
            commit_id, field=f"messages.commit_id: {item.item_id}"
        )
        timestamp = strict_text(timestamp, field="messages.created_at")
        group = item.metadata.get("projection_group")
        if isinstance(group, Mapping) and group.get("ordinal") != group.get("size", 0) - 1:
            return None
        if item.semantic_kind not in {
            SemanticKind.USER_INPUT.value,
            SemanticKind.ASSISTANT_OUTPUT.value,
            SemanticKind.TOOL_CALL.value,
            SemanticKind.TOOL_RESULT.value,
        }:
            return None
        catalog = connection.execute(
            "SELECT content_hash, jsonl_offset, jsonl_length, turn_id "
            "FROM item_catalog WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
        if catalog is None:
            raise RuntimeError(
                "canonical item 已提交但缺少 item_catalog，不能建立 message projection: "
                f"{item.item_id}"
            )
        catalog_hash = strict_text(
            catalog[0], field=f"item_catalog.content_hash: {item.item_id}"
        )
        catalog_offset = strict_non_negative_int(
            catalog[1], field=f"item_catalog.jsonl_offset: {item.item_id}"
        )
        catalog_length = strict_non_negative_int(
            catalog[2], field=f"item_catalog.jsonl_length: {item.item_id}"
        )
        if catalog_length == 0:
            raise RuntimeError(
                f"item_catalog.jsonl_length 必须为正数: {item.item_id}"
            )
        catalog_turn_id = strict_optional_text(
            catalog[3], field=f"item_catalog.turn_id: {item.item_id}"
        )
        if catalog_hash != item.content_hash or catalog_turn_id != item.turn_id:
            raise RuntimeError(
                "canonical item 与 item_catalog identity 不一致，拒绝建立 message projection: "
                f"{item.item_id}"
            )
        with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
            items = read_message_group(
                connection, stream, offset=catalog_offset, length=catalog_length,
                read_item=self._read_v2_item_at,
            )
        message = self._codec().project_message(items)
        raw_data = message.get("data")
        if not isinstance(raw_data, Mapping):
            raise TypeError(f"message projection data 必须是 object: {item.item_id}")
        content_bytes = _json(raw_data.get("content")).encode("utf-8")
        message_id_value = item.metadata.get("projection_message_id")
        if isinstance(message_id_value, str) and message_id_value:
            message_id = message_id_value
        elif item.item_id.startswith("item-"):
            message_id = item.item_id[len("item-") :]
        else:
            message_id = item.item_id
        expected_role = {
            SemanticKind.USER_INPUT.value: "user",
            SemanticKind.ASSISTANT_OUTPUT.value: "assistant",
            SemanticKind.TOOL_CALL.value: "assistant",
            SemanticKind.TOOL_RESULT.value: "tool",
        }[item.semantic_kind]
        if item.wire_role != expected_role:
            raise ValueError(
                "canonical item wire_role 与 message projection 不匹配: "
                f"item={item.item_id}, expected={expected_role}, actual={item.wire_role}"
            )
        expected_message_type = {
            "user": "human",
            "assistant": "ai",
            "tool": "tool",
        }[expected_role]
        if message.get("type") != expected_message_type:
            raise ValueError(
                "canonical item semantic kind 产生了错误的 message projection type: "
                f"item={item.item_id}, expected={expected_message_type}, "
                f"actual={message.get('type')}"
            )
        existing = connection.execute(
            "SELECT message_sequence, turn_id, role, jsonl_offset, jsonl_length, "
            "content_hash FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if existing is not None:
            existing_sequence = strict_non_negative_int(
                existing[0], field=f"messages.message_sequence: {message_id}"
            )
            existing_turn_id = strict_text(
                existing[1],
                field=f"messages.turn_id: {message_id}",
                allow_empty=True,
            )
            existing_role = strict_text(
                existing[2], field=f"messages.role: {message_id}"
            )
            existing_offset = strict_non_negative_int(
                existing[3], field=f"messages.jsonl_offset: {message_id}"
            )
            existing_length = strict_non_negative_int(
                existing[4], field=f"messages.jsonl_length: {message_id}"
            )
            existing_hash = strict_text(
                existing[5], field=f"messages.content_hash: {message_id}"
            )
            if (
                existing_turn_id != (item.turn_id or "")
                or existing_role != expected_role
                or existing_offset != catalog_offset
                or existing_length != catalog_length
                or existing_hash != _hash_bytes(content_bytes)
            ):
                raise ValueError(
                    "message projection identity/content 与 canonical item 冲突: "
                    f"message_id={message_id}, item_id={item.item_id}"
                )
            return existing_sequence, message_id, expected_role

        connection.execute(
            "INSERT INTO messages(message_sequence, message_id, turn_id, role, "
            "jsonl_offset, jsonl_length, content_length, content_hash, visibility, "
            "commit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'visible', ?, ?)",
            (
                message_sequence,
                message_id,
                item.turn_id or "",
                expected_role,
                catalog_offset,
                catalog_length,
                len(content_bytes),
                _hash_bytes(content_bytes),
                commit_id,
                timestamp,
            ),
        )
        self._insert_message_projection(connection, message_sequence, message, timestamp)
        return message_sequence, message_id, expected_role

    def _insert_message_projection(
        self,
        connection: sqlite3.Connection,
        sequence: int,
        value: Mapping[str, object],
        timestamp: str,
    ) -> None:
        codec = self._codec()
        visible = codec.visible_text(value)
        data = value.get("data")
        calls = codec.tool_calls(value)
        reasoning = codec.reasoning_rows(value)
        response_metadata = (
            data.get("response_metadata") if isinstance(data, Mapping) else None
        )
        provider_id = (
            response_metadata.get("provider_id")
            if isinstance(response_metadata, Mapping)
            else None
        )
        if not isinstance(provider_id, str):
            provider_id = None
        message_type = value.get("type")
        connection.execute(
            "INSERT INTO message_projections(message_sequence, text_preview, visible_text, "
            "visible_text_length, visible_text_truncated, has_reasoning, "
            "has_encrypted_reasoning, has_tool_calls, phase, projection_version, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                sequence,
                visible[:512],
                visible,
                len(visible),
                len(visible) >= _VISIBLE_TEXT_LIMIT,
                bool(reasoning),
                any(row.get("kind") == "encrypted" for row in reasoning),
                bool(calls),
                "tool_request"
                if calls
                else "assistant_text"
                if message_type == "ai"
                else None,
                timestamp,
            ),
        )
        for row in reasoning:
            block_index = row.get("content_block_index")
            item_index = row.get("item_index", 0)
            kind = row.get("kind")
            carrier_type = row.get("carrier_type")
            if (
                not isinstance(block_index, int)
                or isinstance(block_index, bool)
                or not isinstance(item_index, int)
                or isinstance(item_index, bool)
                or not isinstance(kind, str)
                or not isinstance(carrier_type, str)
            ):
                continue
            text = row.get("text")
            reasoning_text = row.get("reasoning_text")
            summary_text = row.get("summary_text")
            encrypted_length = row.get("encrypted_length")
            encrypted_hash = row.get("encrypted_hash")
            item_id = row.get("item_id")
            encrypted_length_value = strict_optional_non_negative_int(
                encrypted_length,
                field=f"reasoning_blocks.encrypted_length: {sequence}/{block_index}",
            )
            connection.execute(
                "INSERT INTO reasoning_blocks(message_sequence, content_block_index, "
                "item_index, carrier_type, item_id, reasoning_text, summary_text, "
                "signature_present, encrypted_length, encrypted_hash, provider_id, "
                "projection_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)",
                (
                    sequence,
                    block_index,
                    item_index,
                    carrier_type,
                    item_id if isinstance(item_id, str) else None,
                    reasoning_text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(reasoning_text, str)
                    else text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(text, str) and kind == "reasoning"
                    else None,
                    summary_text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(summary_text, str)
                    else text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(text, str) and kind == "summary"
                    else None,
                    int(row.get("signature_present") is True),
                    encrypted_length_value,
                    encrypted_hash if isinstance(encrypted_hash, str) else None,
                    provider_id,
                ),
            )
        for call_index, call in enumerate(calls):
            call_id = call.get("id")
            name = call.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
            ):
                continue
            args_blob = _json(call.get("args", {})).encode("utf-8")
            connection.execute(
                "INSERT INTO tool_calls(tool_call_id, assistant_message_sequence, "
                "call_index, tool_name, status, argument_length, argument_hash, "
                "summary_text, projection_version) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, 1)",
                (
                    call_id,
                    sequence,
                    call_index,
                    name,
                    len(args_blob),
                    _hash_bytes(args_blob),
                    f"{name} (pending)",
                ),
            )
        if message_type == "tool" and isinstance(data, Mapping):
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                result_blob = _json(data.get("content")).encode("utf-8")
                status = (
                    data.get("status") if isinstance(data.get("status"), str) else "success"
                )
                pending = connection.execute(
                    "SELECT assistant_message_sequence FROM tool_calls "
                    "WHERE tool_call_id = ? AND status = 'pending' "
                    "ORDER BY assistant_message_sequence DESC LIMIT 1",
                    (call_id,),
                ).fetchone()
                if pending is not None:
                    connection.execute(
                        "UPDATE tool_calls SET result_message_sequence = ?, result_length = ?, "
                        "result_hash = ?, status = ?, completed_at = ? "
                        "WHERE tool_call_id = ? AND assistant_message_sequence = ?",
                        (
                            sequence,
                            len(result_blob),
                            _hash_bytes(result_blob),
                            status,
                            timestamp,
                            call_id,
                            strict_non_negative_int(
                                pending[0],
                                field=f"tool_calls.assistant_message_sequence: {call_id}",
                            ),
                        ),
                    )
