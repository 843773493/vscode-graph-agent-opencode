"""v2 checkpoint context view message projection owner。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.records import parse_item_projection_manifest
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
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
        """canonical membership 是唯一选择根；range 只校验既有 view 结构。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        # range 只校验结构，不物化随后被丢弃的祖先 membership；共享祖先与
        # 相同消息范围在本次调用内只检查一次，显式栈也不受 Python 递归深度限制。
        active: set[str] = set()
        completed: set[str] = set()
        checked_spans: set[tuple[int, int]] = set()
        pending = [(view_id, False)]
        while pending:
            current, exiting = pending.pop()
            if exiting:
                active.remove(current)
                completed.add(current)
                continue
            if current in active:
                raise RuntimeError(f"context view 引用形成循环: {current}")
            if current in completed:
                continue
            active.add(current)
            view_row = connection.execute(
                "SELECT view_kind, parent_view_id FROM context_views WHERE view_id = ?",
                (current,),
            ).fetchone()
            if view_row is None:
                raise RuntimeError(f"context view 不存在: {current}")
            view_kind = strict_text(view_row[0], field=f"context_views.view_kind: {current}")
            parent_view_id = strict_optional_text(
                view_row[1], field=f"context_views.parent_view_id: {current}"
            )
            ranges = connection.execute(
                "SELECT source_kind, source_view_id, start_message_sequence, end_message_sequence "
                "FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
                (current,),
            ).fetchall()
            sources: list[str] = []
            for source_kind, source_view, start, end in ranges:
                source_kind = strict_text(
                    source_kind, field=f"context_view_ranges.source_kind: {current}"
                )
                if source_kind == "view":
                    sources.append(strict_text(
                        source_view, field=f"context_view_ranges.source_view_id: {current}"
                    ))
                elif source_kind == "messages":
                    start_value = strict_non_negative_int(start, field=f"context view range start: {current}")
                    end_value = strict_non_negative_int(end, field=f"context view range end: {current}")
                    if start_value == 0 or end_value < start_value:
                        raise RuntimeError(
                            f"context view range 不能反向: view={current}, start={start_value}, end={end_value}"
                        )
                    span = (start_value, end_value)
                    if span not in checked_spans:
                        count = connection.execute(
                            "SELECT COUNT(*) FROM messages WHERE message_sequence BETWEEN ? AND ?", span,
                        ).fetchone()[0]
                        if count != end_value - start_value + 1:
                            raise RuntimeError(
                                f"context view range 引用不存在完整消息范围: view={current}, "
                                f"start={start_value}, end={end_value}"
                            )
                        checked_spans.add(span)
                else:
                    raise RuntimeError(
                        f"context view range source_kind 非法: view={current}, source_kind={source_kind}"
                    )
            if view_kind in {"checkpoint", "checkpoint_messages"} and parent_view_id is not None and parent_view_id not in sources:
                raise FormatDispatchError(
                    f"v2_migration_required: checkpoint view 缺少显式 parent range: view_id={current}"
                )
            pending.append((current, True))
            pending.extend((source, False) for source in reversed(sources))
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
                group = parse_item_projection_manifest(json.loads(projection_manifest))["projection_group"]
                if group is not None and group["ordinal"] < group["size"] - 1:
                    continue
                raise RuntimeError(
                    f"context view canonical item 缺少已提交 message projection: view={view_id}, item={item_id}"
                )
            sequences.append(strict_non_negative_int(sequence, field="messages.message_sequence"))
        return self._include_tool_call_declarations(connection, list(dict.fromkeys(sequences)))
