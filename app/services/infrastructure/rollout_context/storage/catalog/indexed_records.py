"""v2 catalog 的 indexed Turn/item history SQL 查询。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projections import (
    VISIBLE_NORMAL_TURN_PREDICATE,
    _raw_call_id_from_scoped,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )





def _positive_int(value: object, *, field: str) -> int:
    result = strict_non_negative_int(value, field=field)
    if result == 0:
        raise RuntimeError(f"{field} 必须是正整数")
    return result


def _indexed_row(row: tuple[object, ...], *, field: str) -> tuple[int, str, int, int]:
    if len(row) != 4:
        raise RuntimeError(f"{field} 字段数非法: {len(row)}")
    sequence = _positive_int(row[0], field=f"{field}.message_sequence")
    turn_id = strict_text(row[1], field=f"{field}.turn_id")
    offset = strict_non_negative_int(row[2], field=f"{field}.jsonl_offset")
    length = _positive_int(row[3], field=f"{field}.jsonl_length")
    return sequence, turn_id, offset, length


def _turn_span_row(row: tuple[object, ...], *, field: str) -> tuple[str, int, int]:
    if len(row) != 3:
        raise RuntimeError(f"{field} 字段数非法: {len(row)}")
    turn_id = strict_text(row[0], field=f"{field}.turn_id")
    first_sequence = _positive_int(row[1], field=f"{field}.first_message_sequence")
    last_sequence = _positive_int(row[2], field=f"{field}.last_message_sequence")
    if last_sequence < first_sequence:
        raise RuntimeError(f"{field} 消息范围反向: turn_id={turn_id}")
    return turn_id, first_sequence, last_sequence


def _sequence_ranges(
    ranges: Iterable[tuple[str, int, int]] | None,
) -> tuple[tuple[str, int, int], ...]:
    result: list[tuple[str, int, int]] = []
    for index, value in enumerate(ranges or ()):
        if len(value) != 3:
            raise RuntimeError(f"context sequence range 字段数非法: index={index}")
        view_id = strict_text(value[0], field="context sequence range.view_id")
        start = strict_non_negative_int(value[1], field="context sequence range.start")
        end = strict_non_negative_int(value[2], field="context sequence range.end")
        if end < start:
            raise RuntimeError(
                f"context sequence range 不能反向: view={view_id}, start={start}, end={end}"
            )
        result.append((view_id, start, end))
    return tuple(result)


class IndexedRecordQueryMixin:
    """按已提交索引读取 Turn/item 记录，不扫描 v1 或 wire message。"""

    def _records_for_turns(
        self,
        thread_id: str,
        checkpoint_ns: str,
        turn_ids: Iterable[str],
        *,
        message_roles: Iterable[str] | None = None,
        sequences: Iterable[int] | None = None,
        tool_kinds: Iterable[str] | None = None,
        tool_call_ids: Iterable[str] | None = None,
        view_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[tuple[int, str, int, int]]:
        ids = tuple(
            dict.fromkeys(
                strict_text(value, field="indexed_records.turn_id")
                for value in turn_ids
            )
        )
        if not ids:
            return []
        if connection is None:
            with self._connect(
                thread_id, checkpoint_ns, read_only=True
            ) as owned_connection:
                return self._records_for_turns(
                    thread_id,
                    checkpoint_ns,
                    ids,
                    message_roles=message_roles,
                    sequences=sequences,
                    tool_kinds=tool_kinds,
                    tool_call_ids=tool_call_ids,
                    view_id=view_id,
                    connection=owned_connection,
                )
        predicates = ["m.turn_id IN (" + ",".join("?" for _ in ids) + ")"]
        params: list[object] = list(ids)
        roles = tuple(
            dict.fromkeys(
                strict_text(value, field="indexed_records.message_role")
                for value in (message_roles or ())
            )
        )
        if message_roles is not None and not roles:
            # 显式空角色集合表示调用方不需要普通 message 行；不能退化为
            # 不加 role 条件，否则 ToolRow 定点补载会重新读取整轮消息。
            predicates.append("0")
        elif roles:
            predicates.append("m.role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
        selected = {
            _positive_int(value, field="indexed_records.message_sequence")
            for value in sequences or ()
        }
        if selected:
            predicates.append(
                "m.message_sequence IN (" + ",".join("?" for _ in selected) + ")"
            )
            params.extend(sorted(selected))
        view_id = strict_optional_text(view_id, field="indexed_records.view_id")
        if view_id is not None:
            predicates.append(
                "EXISTS (SELECT 1 FROM context_view_turns cvt WHERE cvt.view_id = ? AND cvt.turn_id = m.turn_id)"
            )
            params.append(view_id)
        rows = connection.execute(
            "SELECT m.message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM messages m WHERE "
            + " AND ".join(predicates)
            + " ORDER BY m.message_sequence",
            tuple(params),
        ).fetchall()
        result = {
            _indexed_row(tuple(row), field="indexed_records.message") for row in rows
        }
        selected_tool_call_ids = tuple(
            dict.fromkeys(
                # 前端定点详情携带的是 model-call scoped ID，而 SQLite
                # tool_calls 表按 provider 原始 call ID 保存。两者必须在此
                # 归一化，否则定点请求会查不到任何记录，参数/结果静默为空。
                _raw_call_id_from_scoped(candidate) or candidate
                for value in (tool_call_ids or ())
                for candidate in (strict_text(value, field="indexed_records.tool_call_id"),)
            )
        )
        tool_id_filter = ""
        tool_id_params: tuple[object, ...] = ()
        if selected_tool_call_ids:
            tool_id_filter = (
                " AND tc.tool_call_id IN ("
                + ",".join("?" for _ in selected_tool_call_ids)
                + ")"
            )
            tool_id_params = tuple(selected_tool_call_ids)

            # 一个 assistant message 可能同时声明多个工具；先取命中 call 所在的
            # assistant message，后续 mapper 再按 tool_call_id 过滤 payload。
            assistant_rows = connection.execute(
                "SELECT tc.assistant_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length "
                "FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence "
                "WHERE m.turn_id IN ("
                + ",".join("?" for _ in ids)
                + ")"
                + tool_id_filter,
                (*ids, *tool_id_params),
            ).fetchall()
            result.update(
                _indexed_row(tuple(row), field="indexed_records.tool_call")
                for row in assistant_rows
            )
        if tool_kinds:
            for kind in dict.fromkeys(
                strict_text(value, field="indexed_records.tool_kind")
                for value in tool_kinds
            ):
                if kind == "tool_call":
                    tool_rows = connection.execute(
                        "SELECT tc.assistant_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence WHERE m.turn_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")"
                        + tool_id_filter,
                        (*ids, *tool_id_params),
                    ).fetchall()
                elif kind == "tool_result":
                    tool_rows = connection.execute(
                        "SELECT tc.result_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.result_message_sequence WHERE tc.result_message_sequence IS NOT NULL AND m.turn_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")"
                        + tool_id_filter,
                        (*ids, *tool_id_params),
                    ).fetchall()
                else:
                    raise ValueError(f"未知 indexed record tool kind: {kind}")
                result.update(
                    _indexed_row(tuple(row), field=f"indexed_records.{kind}")
                    for row in tool_rows
                )
        return sorted(result)

    def read_indexed_records(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        sequence_ranges: Iterable[tuple[str, int, int]] | None = None,
        branch_id: str | None = None,
        turn_id: str | None = None,
        kinds: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[dict[str, object]]:
        strict_text(thread_id, field="read_indexed_records.thread_id")
        strict_text(
            checkpoint_ns, field="read_indexed_records.checkpoint_ns", allow_empty=True
        )
        after_sequence = strict_non_negative_int(
            after_sequence, field="read_indexed_records.after_sequence"
        )
        if through_sequence is not None:
            through_sequence = strict_non_negative_int(
                through_sequence, field="read_indexed_records.through_sequence"
            )
            if through_sequence < after_sequence:
                raise ValueError(
                    "read_indexed_records through_sequence 不能小于 after_sequence"
                )
        branch_id = strict_optional_text(
            branch_id, field="read_indexed_records.branch_id"
        )
        del branch_id
        if kinds is not None:
            for kind in kinds:
                if (
                    strict_text(kind, field="read_indexed_records.kind")
                    != "message_append"
                ):
                    raise ValueError(f"未知 indexed record kind: {kind}")
        ranges = _sequence_ranges(sequence_ranges)
        if snapshot is not None:
            self._require_v2_runtime(self._snapshot_connection(snapshot))
        else:
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(
                thread_id, checkpoint_ns, read_only=True
            ) as check_connection:
                self._require_v2_runtime(check_connection)
        if turn_id is None:
            return []
        upper_bound = (
            through_sequence if through_sequence is not None else after_sequence
        )
        rows = self._records_for_turns(
            thread_id,
            checkpoint_ns,
            (turn_id,),
            sequences=range(after_sequence + 1, upper_bound + 1),
            view_id=ranges[-1][0] if ranges else None,
            connection=(self._snapshot_connection(snapshot) if snapshot else None),
        )
        return self._read_record_envelopes(
            thread_id,
            checkpoint_ns,
            rows,
            connection=(self._snapshot_connection(snapshot) if snapshot else None),
        )

    def read_indexed_records_batch(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        turn_ids: Iterable[str],
        sequence_ranges: Iterable[tuple[str, int, int]] | None = None,
        branch_id: str | None = None,
        kinds: Iterable[str] | None = None,
        message_roles: Iterable[str] | None = None,
        tool_kinds: Iterable[str] | None = None,
        tool_call_ids: Iterable[str] | None = None,
        required_sequences: Mapping[str, Iterable[int]] | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        branch_id = strict_optional_text(
            branch_id, field="read_indexed_records_batch.branch_id"
        )
        del branch_id
        if kinds is not None:
            for kind in kinds:
                if (
                    strict_text(kind, field="read_indexed_records_batch.kind")
                    != "message_append"
                ):
                    raise ValueError(f"未知 indexed record kind: {kind}")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        ids = tuple(
            dict.fromkeys(
                strict_text(value, field="read_indexed_records_batch.turn_id")
                for value in turn_ids
            )
        )
        ranges = _sequence_ranges(sequence_ranges)
        view_id = ranges[-1][0] if ranges else None
        result: dict[str, list[dict[str, object]]] = {turn_id: [] for turn_id in ids}
        rows = self._records_for_turns(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            ids,
            message_roles=message_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=tool_call_ids,
            view_id=view_id,
            connection=connection,
        )
        required = required_sequences or {}
        required_values = {
            _positive_int(
                sequence, field="read_indexed_records_batch.required_sequence"
            )
            for sequences in required.values()
            for sequence in sequences
        }
        if required_values:
            rows.extend(
                self._records_for_turns(
                    snapshot.thread_id,
                    snapshot.checkpoint_ns,
                    ids,
                    sequences=required_values,
                    view_id=view_id,
                    connection=connection,
                )
            )
        unique_rows = {
            (sequence, turn_id, offset, length): (sequence, turn_id, offset, length)
            for sequence, turn_id, offset, length in rows
        }
        records = self._read_record_envelopes(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            sorted(unique_rows.values()),
            connection=connection,
        )
        for record in records:
            turn_id = record.get("turn_id")
            if not isinstance(turn_id, str) or turn_id not in result:
                raise RuntimeError("indexed record 返回未知或非法 turn_id")
            result[turn_id].append(record)
        return result

    def _read_record_envelopes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        rows: Iterable[tuple[int, str, int, int]],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        row_values = tuple(rows)
        if not row_values:
            return []
        row_values = tuple(
            _indexed_row(tuple(row), field="indexed_records.input")
            for row in row_values
        )
        result: list[dict[str, object]] = []
        owned_connection = None
        if connection is None:
            owned_connection = self._connect(thread_id, checkpoint_ns, read_only=True)
            connection = owned_connection
        try:
            self._require_v2_runtime(connection)
            indexed_rows = tuple(row_values)
            message_rows = connection.execute(
                "SELECT message_sequence, message_id, turn_id, role, created_at "
                "FROM messages WHERE message_sequence IN ("
                + ",".join("?" for _ in indexed_rows)
                + ")",
                tuple(row[0] for row in indexed_rows),
            ).fetchall()
            messages_by_sequence = {
                _positive_int(row[0], field="messages.message_sequence"): (
                    strict_text(row[1], field="messages.message_id"),
                    strict_text(row[2], field="messages.turn_id"),
                    strict_text(row[3], field="messages.role"),
                    strict_text(row[4], field="messages.created_at"),
                )
                for row in message_rows
            }
            with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
                for sequence, turn_id_value, offset, length in indexed_rows:
                    items = read_message_group(
                        connection,
                        stream,
                        offset=offset,
                        length=length,
                        read_item=self._read_v2_item_at,
                    )
                    message = self._codec().project_message(items)
                    message_identity = messages_by_sequence.get(sequence)
                    if message_identity is None:
                        raise RuntimeError(
                            f"indexed message 缺少 SQLite identity: sequence={sequence}"
                        )
                    message_id, message_turn_id, role, created_at = message_identity
                    message = self._hydrate_message_identity(
                        message,
                        message_id=message_id,
                        turn_id=message_turn_id,
                        created_at=created_at,
                    )
                    if message_turn_id != turn_id_value:
                        raise RuntimeError(
                            "indexed message 的 Turn identity 不一致: "
                            f"sequence={sequence}"
                        )
                    result.append(
                        {
                            "kind": "message_append",
                            "sequence": sequence,
                            "_indexed_sequence": sequence,
                            "turn_id": message_turn_id,
                            "message_id": message_id,
                            "message": message,
                            "role": role,
                        }
                    )
        finally:
            if owned_connection is not None:
                owned_connection.close()
        return result

    def indexed_turn_spans(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        branch_id: str | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[tuple[str, int, int]]:
        strict_text(thread_id, field="indexed_turn_spans.thread_id")
        strict_text(
            checkpoint_ns, field="indexed_turn_spans.checkpoint_ns", allow_empty=True
        )
        after_sequence = strict_non_negative_int(
            after_sequence, field="indexed_turn_spans.after_sequence"
        )
        if through_sequence is not None:
            through_sequence = strict_non_negative_int(
                through_sequence, field="indexed_turn_spans.through_sequence"
            )
            if through_sequence < after_sequence:
                raise ValueError(
                    "indexed_turn_spans through_sequence 不能小于 after_sequence"
                )
        branch_id = strict_optional_text(
            branch_id, field="indexed_turn_spans.branch_id"
        )
        del branch_id

        def read(
            connection: sqlite3.Connection, active_branch_id: str
        ) -> list[tuple[str, int, int]]:
            self._require_v2_runtime(connection)
            active_branch_id = strict_text(
                active_branch_id, field="indexed_turn_spans.active_branch_id"
            )
            view_row = connection.execute(
                "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                (active_branch_id,),
            ).fetchone()
            if view_row is None or view_row[0] is None:
                raise RuntimeError(
                    "active branch 缺少 context view，拒绝从全局 turns 推导历史"
                )
            view_id = strict_text(view_row[0], field="indexed_turn_spans.head_view_id")
            rows = connection.execute(
                f"SELECT t.turn_id, t.first_message_sequence, t.last_message_sequence "
                f"FROM context_view_turns AS cvt JOIN turns AS t ON t.turn_id = cvt.turn_id "
                f"WHERE cvt.view_id = ? AND t.last_message_sequence > ? "
                f"AND (? IS NULL OR t.first_message_sequence <= ?) "
                f"AND {VISIBLE_NORMAL_TURN_PREDICATE} "
                "ORDER BY cvt.logical_turn_ordinal",
                (view_id, after_sequence, through_sequence, through_sequence),
            ).fetchall()
            return [
                _turn_span_row(tuple(row), field="indexed_turn_spans.row")
                for row in rows
            ]

        if snapshot is not None:
            connection = self._snapshot_connection(snapshot)
            return read(connection, snapshot.manifest.active_branch_id)
        else:
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
                branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                if branch_row is None or branch_row[0] is None:
                    raise RuntimeError(
                        "rollout namespace 缺少 active branch，拒绝推导历史"
                    )
                return read(
                    connection,
                    strict_text(
                        branch_row[0],
                        field="checkpoint_namespace_state.active_branch_id",
                    ),
                )

    def indexed_turn_spans_for_ranges(
        self, snapshot: RolloutReadSnapshot, ranges: Iterable[tuple[str, int, int]]
    ) -> list[tuple[str, int, int]]:
        values = _sequence_ranges(ranges)
        if not values:
            return []
        view_id = values[-1][0]
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id WHERE cvt.view_id = ? AND {VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal",
            (view_id,),
        ).fetchall()
        return [
            _turn_span_row(tuple(row), field="indexed_turn_spans_for_ranges.row")
            for row in rows
        ]

    def context_turn_count(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> int:
        """返回一个逻辑 context view 的 Turn 数量，不读取消息正文。"""
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        view_id = strict_text(view_id, field="context_turn_count.view_id")
        row = connection.execute(
            f"SELECT COUNT(*) FROM context_view_turns AS cvt WHERE cvt.view_id = ? AND {VISIBLE_NORMAL_TURN_PREDICATE}",
            (view_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("context_turn_count COUNT 查询未返回结果")
        return strict_non_negative_int(row[0], field="context_turn_count.count")
