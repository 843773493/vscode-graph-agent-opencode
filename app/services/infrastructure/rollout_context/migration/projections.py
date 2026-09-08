"""legacy migration 生成 v2 派生 message projection 的 owner。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hashing import canonical_json_bytes, payload_content_length
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LegacyMigrationProjectionMixin:
    """只负责 migration target 的一次性派生 projection 安装。"""

    @staticmethod
    def _migration_projection_text(value: object) -> str:
        """为 v2 派生 message projection 提取有界可见文本。"""
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
            content = value.get("content")
            if content is not None:
                return LegacyMigrationProjectionMixin._migration_projection_text(
                    content
                )
            return ""
        if isinstance(value, (list, tuple)):
            return "".join(
                LegacyMigrationProjectionMixin._migration_projection_text(item)
                for item in value
            )
        return ""

    def _install_migration_message_projections(
        self,
        target_thread_id: str,
        *,
        checkpoint_ns: str,
        items: tuple[CanonicalItemRecord, ...],
    ) -> None:
        """为导入 item 建立派生 history projection，不复制 canonical 正文。"""
        target_thread_id = strict_text(target_thread_id, field="migration.target_session_id")
        strict_text(checkpoint_ns, field="migration.checkpoint_ns", allow_empty=True)
        if any(not isinstance(item, CanonicalItemRecord) for item in items):
            raise TypeError("migration projection items 必须是 CanonicalItemRecord")
        visible_items = tuple(
            item
            for item in items
            if item.turn_id is not None
            and item.semantic_kind
            in {
                SemanticKind.USER_INPUT,
                SemanticKind.ASSISTANT_OUTPUT,
                SemanticKind.TOOL_RESULT,
            }
        )
        visible_items = tuple(sorted(visible_items, key=lambda item: item.item_sequence))
        turn_ids = {item.turn_id for item in visible_items}
        if len(turn_ids) > 1:
            raise RuntimeError("migration projection 不得把多个 Turn 合并为一个 turns 行")
        if not visible_items:
            return
        with self._connect(target_thread_id, checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in visible_items:
                row = connection.execute(
                    "SELECT jsonl_offset, jsonl_length, commit_id, payload_length, metadata_json FROM item_catalog WHERE item_id = ?",
                    (item.item_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        f"migration item catalog 缺少已提交 item: {item.item_id}"
                    )
                offset = strict_non_negative_int(
                    row[0], field="item_catalog.jsonl_offset"
                )
                length = strict_non_negative_int(
                    row[1], field="item_catalog.jsonl_length"
                )
                if length == 0:
                    raise RuntimeError(
                        f"migration item JSONL length 为 0: {item.item_id}"
                    )
                commit_id = strict_non_negative_int(
                    row[2], field="item_catalog.commit_id"
                )
                payload_length = strict_non_negative_int(
                    row[3], field="item_catalog.payload_length"
                )
                if payload_length != payload_content_length(
                    item.payload_kind, item.payload
                ):
                    raise RuntimeError(
                        f"migration item payload_length 不一致: {item.item_id}"
                    )
                metadata_text = strict_text(
                    row[4], field="item_catalog.metadata_json"
                )
                try:
                    metadata = json.loads(metadata_text)
                except (TypeError, json.JSONDecodeError) as error:
                    raise RuntimeError(
                        f"migration item metadata JSON 非法: {item.item_id}"
                    ) from error
                if not isinstance(metadata, Mapping):
                    raise TypeError(
                        f"migration item metadata 必须是 object: {item.item_id}"
                    )
                if canonical_json_bytes(metadata).decode("utf-8") != metadata_text:
                    raise RuntimeError(
                        f"migration item metadata 不是 canonical JSON: {item.item_id}"
                    )
                projection_message_id = (
                    metadata.get("projection_message_id")
                    if isinstance(metadata, Mapping)
                    else None
                )
                if not isinstance(projection_message_id, str) or not projection_message_id:
                    raise RuntimeError(
                        "migration item 缺少 target-local projection_message_id: "
                        f"{item.item_id}"
                    )
                role = (
                    "user"
                    if item.semantic_kind == SemanticKind.USER_INPUT
                    else "tool"
                    if item.semantic_kind == SemanticKind.TOOL_RESULT
                    else "assistant"
                )
                content = (
                    item.payload.get("content")
                    if item.semantic_kind == SemanticKind.TOOL_RESULT
                    and isinstance(item.payload, Mapping)
                    else item.payload
                )
                content_bytes = canonical_json_bytes(content)
                text = self._migration_projection_text(content)
                result = connection.execute(
                    "INSERT INTO messages(message_sequence, message_id, turn_id, role, jsonl_offset, jsonl_length, content_length, content_hash, visibility, commit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'visible', ?, ?)",
                    (
                        item.item_sequence,
                        projection_message_id,
                        item.turn_id,
                        role,
                        offset,
                        length,
                        len(content_bytes),
                        hashlib.sha256(content_bytes).hexdigest(),
                        commit_id,
                        item.created_at,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"migration message projection 未写入: {item.item_id}"
                    )
                result = connection.execute(
                    "INSERT INTO message_projections(message_sequence, text_preview, visible_text, visible_text_length, visible_text_truncated, has_reasoning, has_encrypted_reasoning, has_tool_calls, phase, projection_version, updated_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, 1, ?)",
                    (
                        item.item_sequence,
                        text[:512],
                        text,
                        len(text),
                        int(len(text) >= 64 * 1024),
                        int(item.semantic_kind == SemanticKind.TOOL_CALL),
                        "tool_result"
                        if item.semantic_kind == SemanticKind.TOOL_RESULT
                        else "assistant_text"
                        if item.semantic_kind == SemanticKind.ASSISTANT_OUTPUT
                        else "user_input",
                        _now(),
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"migration message projection detail 未写入: {item.item_id}"
                    )
            turn_id = strict_text(visible_items[0].turn_id, field="item.turn_id")
            turn_row = connection.execute(
                "SELECT turn_ordinal, source_branch_id, status, created_at, updated_at FROM turn_records WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if turn_row is None:
                raise RuntimeError(f"migration TurnRecord 缺失: {turn_id}")
            first_sequence = min(item.item_sequence for item in visible_items)
            last_sequence = max(item.item_sequence for item in visible_items)
            user_item = next(
                item
                for item in visible_items
                if item.semantic_kind == SemanticKind.USER_INPUT
            )
            turn_ordinal = strict_non_negative_int(
                turn_row[0], field="turn_records.turn_ordinal"
            )
            if turn_ordinal == 0:
                raise RuntimeError("migration turn_ordinal 不能为 0")
            branch_id = strict_text(
                turn_row[1], field="turn_records.source_branch_id"
            )
            source_status = strict_text(
                turn_row[2], field="turn_records.status"
            )
            allowed_statuses = {
                "open",
                "active",
                "completed",
                "completed_empty",
                "interrupted",
                "cancelled",
                "failed",
                "unknown",
            }
            if source_status not in allowed_statuses:
                raise RuntimeError(
                    f"migration TurnRecord.status 非法: {turn_id}/{source_status}"
                )
            created_at = strict_text(turn_row[3], field="turn_records.created_at")
            updated_at = strict_text(turn_row[4], field="turn_records.updated_at")
            history_status = "running" if source_status in {"open", "active"} else source_status
            result = connection.execute(
                "INSERT INTO turns(turn_id, turn_ordinal, turn_kind, branch_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at) VALUES (?, ?, 'normal', ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (
                    turn_id,
                    turn_ordinal,
                    branch_id,
                    first_sequence,
                    last_sequence,
                    user_item.item_sequence,
                    history_status,
                    created_at,
                    updated_at,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"migration turns projection 未写入: {turn_id}")
            active_view = connection.execute(
                "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                (branch_id,),
            ).fetchone()
            if active_view is not None and active_view[0] is not None:
                active_view_id = strict_text(
                    active_view[0], field="branches.head_view_id"
                )
                result = connection.execute(
                    "UPDATE context_view_turns SET user_message_sequence = ? WHERE view_id = ? AND turn_id = ?",
                    (user_item.item_sequence, active_view_id, turn_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"migration context view Turn 未更新: {turn_id}"
                    )
            connection.commit()


__all__ = ["LegacyMigrationProjectionMixin"]
