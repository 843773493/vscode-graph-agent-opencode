"""v2 checkpoint context view message projection owner。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from app.domain.itemized.identity.item_projection import ItemProjectionManifest
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


class RolloutViewSequenceMixin:
    def _view_message_sequences(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        *,
        session_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[int]:
        if connection is None:
            if session_id is None:
                raise ValueError("读取 context view message sequences 必须显式提供 session_id")
            with self.read_connection(session_id, thread_id, checkpoint_ns) as owned_connection:
                return self._view_message_sequences_from_connection(
                    owned_connection, view_id,
                )
        return self._view_message_sequences_from_connection(connection, view_id)
    @staticmethod
    def _include_tool_call_declarations(
        connection: sqlite3.Connection,
        sequences: Sequence[int],
    ) -> list[int]:
        """确保 view 中的工具结果始终带有对应的 assistant 工具声明。

        旧版本在增量 checkpoint 只携带 ToolMessage，或在并行工具组被拆成
        多段 delta 时，可能把结果范围写入 view，却漏掉父 view 中的
        AIMessage。Responses provider 会把这种状态编码为孤立
        ``function_call_output`` 并直接拒绝请求。tool_calls projection 是
        canonical 消息之外的配对索引；读取时补回声明序号不会修改 JSONL，
        也不会把缺少配对索引的损坏结果伪装成成功。
        """
        ordered = list(
            dict.fromkeys(
                strict_non_negative_int(sequence, field="message_sequence")
                for sequence in sequences
            )
        )
        if not ordered:
            return []
        rows: list[sqlite3.Row | tuple[object, ...]] = []
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    "SELECT assistant_message_sequence FROM tool_calls "
                    f"WHERE result_message_sequence IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
            )
        present = set(ordered)
        missing: set[int] = set()
        for row in rows:
            if row[0] is None:
                continue
            assistant_sequence = strict_non_negative_int(
                row[0], field="tool_calls.assistant_message_sequence"
            )
            if assistant_sequence not in present:
                missing.add(assistant_sequence)
        if not missing:
            return ordered
        return sorted((*present, *missing))
    def _view_message_sequences_from_connection(
        self,
        connection: sqlite3.Connection,
        view_id: str,
    ) -> list[int]:
        """仅按目标 canonical membership 选取消息；历史 range 图留给完整审计。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        # 消息选集已冻结在目标 membership；祖先 range 图只在恢复和完整审计时遍历。
        if connection.execute("SELECT 1 FROM context_views WHERE view_id = ?", (view_id,)).fetchone() is None:
            raise RuntimeError(f"context view 不存在: {view_id}")
        rows = connection.execute(
            "SELECT member.item_id, item.semantic_kind, item.projection_manifest_json, "
            "message.message_sequence FROM context_view_items AS member "
            "JOIN item_catalog AS item ON item.item_id = member.item_id "
            "LEFT JOIN messages AS message ON message.jsonl_offset = item.jsonl_offset "
            "AND message.jsonl_length = item.jsonl_length "
            "WHERE member.view_id = ? AND member.visible = 1 ORDER BY member.logical_item_ordinal",
            (view_id,),
        ).fetchall()
        sequences: list[int] = []
        for item_id, semantic_kind, projection_manifest, sequence in rows:
            if sequence is None:
                if semantic_kind not in {"user_input", "assistant_output", "tool_call", "tool_result"}:
                    continue
                group = ItemProjectionManifest.from_dict(json.loads(projection_manifest)).projection_group
                if group is not None and group.ordinal < group.size - 1:
                    continue
                raise RuntimeError(
                    f"context view canonical item 缺少已提交 message projection: view={view_id}, item={item_id}"
                )
            sequences.append(strict_non_negative_int(sequence, field="messages.message_sequence"))
        return self._include_tool_call_declarations(connection, list(dict.fromkeys(sequences)))
