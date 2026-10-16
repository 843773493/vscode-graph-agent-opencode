"""LangChain message projection 的 checkpoint owner。

canonical item 是 JSONL/catalog 的唯一正文事实；本模块只把已提交 item
映射成 history/message locator 和 bounded tool/reasoning 索引。MessageCodec
由 checkpoint 组装层注入，本模块不自行实现 LangChain 纯映射。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from app.core.hashing import sha256_hex as _hash_bytes
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

_VISIBLE_TEXT_LIMIT = 64 * 1024


class RolloutMessageProjectionMixin:
    """维护 message、tool 和 reasoning 的派生 projection。"""

    def _materialize_canonical_message_projection(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
        *,
        thread_id: str,
        session_id: str,
        checkpoint_ns: str,
        commit_id: int,
        message_sequence: int,
        timestamp: str,
    ) -> tuple[int, str, str] | None:
        """把可映射的 canonical item 镜像成同一事务内的 message projection。

        ``timestamp`` 是调用方提供的消息投影时间：普通 writer 传本次
        ``_now``/timestamp，fork 的 ``copy_items`` 沿用源 ``source_messages``
        行的 ``row[5]``，保留原消息投影时间，不代表 canonical item 创建时间
        或复制提交时刻。
        """
        message_sequence = strict_non_negative_int(
            message_sequence, field=f"messages.message_sequence: {item.item_id}"
        )
        if message_sequence == 0:
            raise ValueError(f"message_sequence 必须为正数: {item.item_id}")
        commit_id = strict_non_negative_int(
            commit_id, field=f"messages.commit_id: {item.item_id}"
        )
        timestamp = strict_text(timestamp, field="messages.created_at")
        group = item.projection_group
        if group is not None and group.ordinal != group.size - 1:
            return None
        if item.semantic_kind not in {
            SemanticKind.USER_INPUT.value,
            SemanticKind.RUNTIME_NOTICE.value,
            SemanticKind.ASSISTANT_OUTPUT.value,
            SemanticKind.TOOL_CALL.value,
            SemanticKind.TOOL_RESULT.value,
        }:
            return None
        catalog = connection.execute(
            "SELECT content_hash, jsonl_offset, jsonl_length, turn_id, execution_id "
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
        catalog_execution_id = strict_text(
            catalog[4], field=f"item_catalog.execution_id: {item.item_id}"
        )
        if (
            catalog_hash != item.content_hash
            or catalog_turn_id != item.turn_id
        ):
            raise RuntimeError(
                "canonical item 与 item_catalog identity 不一致，拒绝建立 message projection: "
                f"{item.item_id}"
            )
        with self._storage.jsonl_path(
            session_id,
            checkpoint_ns,
            thread_id=thread_id,
        ).open("rb") as stream:
            items = read_message_group(
                connection, stream, offset=catalog_offset, length=catalog_length,
                read_item=self._storage._read_v2_item_at,
            )
        message = self._storage._codec().project_message(items)
        raw_data = message.get("data")
        if not isinstance(raw_data, Mapping):
            raise TypeError(f"message projection data 必须是 object: {item.item_id}")
        content_bytes = _json(raw_data.get("content")).encode("utf-8")
        message_id = strict_text(
            raw_data.get("id"), field=f"message projection.id: {item.item_id}"
        )
        expected_role = {
            SemanticKind.USER_INPUT.value: "user",
            SemanticKind.RUNTIME_NOTICE.value: "user",
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
            "SELECT message_sequence, turn_id, execution_id, role, jsonl_offset, "
            "jsonl_length, content_hash FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if existing is not None:
            existing_sequence = strict_non_negative_int(
                existing[0], field=f"messages.message_sequence: {message_id}"
            )
            existing_turn_id = strict_optional_text(
                existing[1],
                field=f"messages.turn_id: {message_id}",
            )
            existing_execution_id = strict_text(
                existing[2], field=f"messages.execution_id: {message_id}"
            )
            existing_role = strict_text(
                existing[3], field=f"messages.role: {message_id}"
            )
            existing_offset = strict_non_negative_int(
                existing[4], field=f"messages.jsonl_offset: {message_id}"
            )
            existing_length = strict_non_negative_int(
                existing[5], field=f"messages.jsonl_length: {message_id}"
            )
            existing_hash = strict_text(
                existing[6], field=f"messages.content_hash: {message_id}"
            )
            if (
                existing_turn_id != item.turn_id
                or existing_execution_id != catalog_execution_id
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
            "INSERT INTO messages(message_sequence, message_id, turn_id, execution_id, "
            "role, jsonl_offset, jsonl_length, content_length, content_hash, "
            "visibility, commit_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_sequence,
                message_id,
                item.turn_id,
                catalog_execution_id,
                expected_role,
                catalog_offset,
                catalog_length,
                len(content_bytes),
                _hash_bytes(content_bytes),
                "internal" if item.semantic_kind == SemanticKind.RUNTIME_NOTICE else "visible",
                commit_id,
                timestamp,
            ),
        )
        self._insert_message_projection(
            connection, message_sequence, message, timestamp, canonical_items=items,
        )
        return message_sequence, message_id, expected_role

    def _insert_message_projection(
        self,
        connection: sqlite3.Connection,
        sequence: int,
        value: Mapping[str, object],
        timestamp: str,
        *,
        canonical_items: Sequence[CanonicalItemRecord],
    ) -> None:
        """写入消息派生视图，``timestamp`` 用于该投影的 ``updated_at``。

        时间沿用上游消息投影：普通 writer 传本次 ``_now``/timestamp，fork
        的 ``copy_items`` 传源 ``source_messages`` 行的 ``row[5]``，保留原
        消息投影时间，不重新取复制提交时刻。
        """
        codec = self._storage._codec()
        visible = codec.visible_text(value)
        data = value.get("data")
        # SQL 配对保存 canonical 调用身份；临时 LangChain wire 的 raw ID
        # 不能覆盖 stream scope，两个 sink 的配对仍各自绑定真实 carrier。
        calls = [
            call for item in canonical_items if item.semantic_kind == SemanticKind.TOOL_CALL
            for call in item.payload.get("tool_calls", [item.payload])
        ]
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
            call_id = call.get("id") or call.get("tool_call_id")
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
            canonical_row = connection.execute(
                "SELECT item.status, item.projection_manifest_json, message.execution_id, message.turn_id "
                "FROM messages AS message JOIN item_catalog AS item "
                "ON item.jsonl_offset = message.jsonl_offset AND item.jsonl_length = message.jsonl_length "
                "WHERE message.message_sequence = ?",
                (sequence,),
            ).fetchone()
            if canonical_row is None:
                raise RuntimeError(f"tool message 缺少真实 canonical locator: {sequence}")
            if canonical_row[0] != "completed":
                return
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                result_blob = _json(data.get("content")).encode("utf-8")
                status = (
                    data.get("status") if isinstance(data.get("status"), str) else "success"
                )
                projection = parse_item_projection_manifest(
                    json.loads(canonical_row[1]),
                )["projection_identity"]
                identity = ItemProjectionIdentity.from_dict(projection) if projection is not None else None
                model_call_id = identity.model_call_id if identity is not None else None
                checkpoint_result = identity is not None and identity.projection_message_id is not None
                raw_call_id = provider_tool_call_id(identity, call_id)
                scoped_call_id = f"{model_call_id}:tool-call:{raw_call_id}" if model_call_id is not None else raw_call_id
                paired_candidates = connection.execute(
                    "SELECT call.tool_call_id, call.assistant_message_sequence, item.projection_manifest_json, call.status "
                    "FROM tool_calls AS call JOIN messages AS message "
                    "ON message.message_sequence = call.assistant_message_sequence "
                    "JOIN item_catalog AS item ON item.jsonl_offset = message.jsonl_offset "
                    "AND item.jsonl_length = message.jsonl_length "
                    "WHERE call.tool_call_id IN (?, ?) "
                    "AND message.execution_id = ? AND message.turn_id IS ?",
                    (raw_call_id, scoped_call_id, canonical_row[2], canonical_row[3]),
                ).fetchall()
                for pending_call_id, assistant_sequence, projection_manifest, call_status in paired_candidates:
                    pending_projection = parse_item_projection_manifest(
                        json.loads(projection_manifest),
                    )["projection_identity"]
                    pending_identity = ItemProjectionIdentity.from_dict(pending_projection) if pending_projection is not None else None
                    pending_model_call_id = pending_identity.model_call_id if pending_identity is not None else None
                    checkpoint_call = pending_identity is not None and pending_identity.projection_message_id is not None
                    if pending_model_call_id != model_call_id or (
                        call_status != "pending" and checkpoint_call != checkpoint_result
                    ):
                        continue
                    # 同 scope 的真实 terminal 先闭合 pending 配对；同类
                    # carrier 到达后更新引用，已闭合引用不再被另一 sink 改回。
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
                            pending_call_id,
                            strict_non_negative_int(
                                assistant_sequence,
                                field=f"tool_calls.assistant_message_sequence: {call_id}",
                            ),
                        ),
                    )
