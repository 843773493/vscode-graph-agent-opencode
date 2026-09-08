"""v2 context view message-locator query owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutMessageViewMixin:
    """只按已提交 context view 查询消息 locator，不实现 projection。"""

    def _messages_for_view(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[object]:
        view_id = strict_text(view_id, field="context_views.view_id")
        sequences = self._view_message_sequences(
            thread_id,
            checkpoint_ns,
            view_id,
            set(),
            connection=connection,
        )
        if not sequences:
            return []
        values = self._read_messages(
            thread_id,
            checkpoint_ns,
            sequences,
            connection=connection,
        )
        return [self._codec().from_dict(value) for value in values]


    def _view_message_sequences(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        visited: set[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[int]:
        view_id = strict_text(view_id, field="context_views.view_id")
        if connection is None:
            with self._connect(thread_id, checkpoint_ns) as owned_connection:
                return self._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    view_id,
                    visited,
                    connection=owned_connection,
                )
        if view_id in visited:
            raise RuntimeError(f"context view 引用形成循环: {view_id}")
        visited.add(view_id)
        ranges = connection.execute(
            "SELECT source_kind, source_view_id, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        view_row = connection.execute(
            "SELECT view_kind, parent_view_id FROM context_views WHERE view_id = ?",
            (view_id,),
        ).fetchone()
        if view_row is None:
            raise RuntimeError(f"context view 不存在: {view_id}")
        view_kind = strict_text(view_row[0], field=f"context_views.view_kind: {view_id}")
        parent_view_id = strict_optional_text(
            view_row[1], field=f"context_views.parent_view_id: {view_id}"
        )
        result: list[int] = []
        has_parent_range = False
        for source_kind, source_view, start, end in ranges:
            source_kind = strict_text(
                source_kind, field=f"context_view_ranges.source_kind: {view_id}"
            )
            if source_kind == "view":
                source_view = strict_text(
                    source_view,
                    field=f"context_view_ranges.source_view_id: {view_id}",
                )
                result.extend(
                    self._view_message_sequences(
                        thread_id,
                        checkpoint_ns,
                        source_view,
                        visited.copy(),
                        connection=connection,
                    )
                )
                if (
                    view_row is not None
                    and parent_view_id is not None
                    and source_view == parent_view_id
                ):
                    has_parent_range = True
            elif source_kind == "messages":
                start_value = strict_non_negative_int(
                    start,
                    field=f"context view range start: {view_id}",
                )
                end_value = strict_non_negative_int(
                    end,
                    field=f"context view range end: {view_id}",
                )
                if start_value == 0 or end_value == 0 or end_value < start_value:
                    raise RuntimeError(
                        f"context view range 不能反向: view={view_id}, "
                        f"start={start_value}, end={end_value}"
                    )
                rows = connection.execute(
                    "SELECT message_sequence FROM messages WHERE message_sequence BETWEEN ? AND ? ORDER BY message_sequence",
                    (start_value, end_value),
                ).fetchall()
                expected_count = end_value - start_value + 1
                if len(rows) != expected_count:
                    raise RuntimeError(
                        "context view range 引用不存在完整消息范围: "
                        f"view={view_id}, start={start_value}, end={end_value}"
                    )
                result.extend(
                    strict_non_negative_int(
                        row[0], field="messages.message_sequence"
                    )
                    for row in rows
                )
            else:
                raise RuntimeError(
                    f"context view range source_kind 非法: view={view_id}, "
                    f"source_kind={source_kind}"
                )
        if (
            view_kind == "checkpoint"
            and parent_view_id is not None
            and not has_parent_range
        ):
            raise FormatDispatchError(
                "v2_migration_required: checkpoint view 缺少显式 parent range: "
                f"view_id={view_id}"
            )
        return self._include_tool_call_declarations(connection, result)


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
        missing = {
            strict_non_negative_int(
                row[0], field="tool_calls.assistant_message_sequence"
            )
            for row in rows
            if row[0] is not None
            and strict_non_negative_int(
                row[0], field="tool_calls.assistant_message_sequence"
            )
            not in present
        }
        if not missing:
            return ordered
        return sorted((*present, *missing))


    def _view_message_sequences_from_connection(
        self,
        connection: sqlite3.Connection,
        view_id: str,
        visited: set[str] | None = None,
    ) -> list[int]:
        """在单个 SQLite snapshot 中解析 view，供离线 compaction 使用。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        seen = set(visited or ())
        if view_id in seen:
            raise RuntimeError(f"context view 引用形成循环: {view_id}")
        seen.add(view_id)
        ranges = connection.execute(
            "SELECT source_kind, source_view_id, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        view_row = connection.execute(
            "SELECT view_kind, parent_view_id FROM context_views WHERE view_id = ?",
            (view_id,),
        ).fetchone()
        if view_row is None:
            raise RuntimeError(f"context view 不存在: {view_id}")
        view_kind = strict_text(view_row[0], field=f"context_views.view_kind: {view_id}")
        parent_view_id = strict_optional_text(
            view_row[1], field=f"context_views.parent_view_id: {view_id}"
        )
        result: list[int] = []
        has_parent_range = False
        for source_kind, source_view, start, end in ranges:
            source_kind = strict_text(
                source_kind, field=f"context_view_ranges.source_kind: {view_id}"
            )
            if source_kind == "view":
                source_view = strict_text(
                    source_view,
                    field=f"context_view_ranges.source_view_id: {view_id}",
                )
                result.extend(
                    self._view_message_sequences_from_connection(
                        connection, source_view, seen
                    )
                )
                if (
                    view_row is not None
                    and parent_view_id is not None
                    and source_view == parent_view_id
                ):
                    has_parent_range = True
            elif source_kind == "messages":
                start_value = strict_non_negative_int(
                    start,
                    field=f"context view range start: {view_id}",
                )
                end_value = strict_non_negative_int(
                    end,
                    field=f"context view range end: {view_id}",
                )
                if start_value == 0 or end_value == 0 or end_value < start_value:
                    raise RuntimeError(
                        f"context view range 不能反向: view={view_id}, "
                        f"start={start_value}, end={end_value}"
                    )
                rows = connection.execute(
                    "SELECT message_sequence FROM messages WHERE message_sequence BETWEEN ? AND ? ORDER BY message_sequence",
                    (start_value, end_value),
                ).fetchall()
                expected_count = end_value - start_value + 1
                if len(rows) != expected_count:
                    raise RuntimeError(
                        "context view range 引用不存在完整消息范围: "
                        f"view={view_id}, start={start_value}, end={end_value}"
                    )
                result.extend(
                    strict_non_negative_int(
                        row[0], field="messages.message_sequence"
                    )
                    for row in rows
                )
            else:
                raise RuntimeError(
                    f"context view range source_kind 非法: view={view_id}, "
                    f"source_kind={source_kind}"
                )
        if (
            view_kind == "checkpoint"
            and parent_view_id is not None
            and not has_parent_range
        ):
            raise FormatDispatchError(
                "v2_migration_required: checkpoint view 缺少显式 parent range: "
                f"view_id={view_id}"
            )
        return self._include_tool_call_declarations(connection, result)
