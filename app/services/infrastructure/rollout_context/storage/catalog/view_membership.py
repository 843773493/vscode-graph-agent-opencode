"""canonical catalog 的 active context view item membership。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.rollout_context.storage.catalog.view_ancestry import (
    insert_view_jumps,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    append_members,
    clone_members,
    replace_members,
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


def _insert_view_ranges(
    connection: sqlite3.Connection, view_id: str, parent_view_id: str | None,
    sequence_values: Sequence[int], view_kind: str,
) -> None:
    """登记父来源和本次消息范围；克隆成员仍须保留完整的重试与审计证据。"""
    ranges: list[tuple[int, int]] = []
    for sequence in sequence_values:
        if not ranges or sequence != ranges[-1][1] + 1:
            ranges.append((sequence, sequence))
        else:
            ranges[-1] = (ranges[-1][0], sequence)
    range_index = 0
    if parent_view_id and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
        # range 保留增量 checkpoint 的父来源证据，供完整审计检查；
        # 实际消息选集已冻结在 membership，不在普通读取时展开祖先图。
        range_cursor = connection.execute(
            "INSERT INTO context_view_ranges(view_id, range_index, source_kind, source_view_id, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'view', ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)",
            (view_id, range_index, parent_view_id, range_index),
        )
        if range_cursor.rowcount != 1:
            raise RuntimeError(f"context view parent range 插入失败: {view_id}")
        range_index += 1
    for start_sequence, end_sequence in ranges:
        range_cursor = connection.execute(
            "INSERT INTO context_view_ranges(view_id, range_index, source_kind, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'messages', ?, ?, ?, ?, ?, NULL, NULL)",
            (
                view_id,
                range_index,
                start_sequence,
                end_sequence,
                start_sequence,
                end_sequence,
                range_index,
            ),
        )
        if range_cursor.rowcount != 1:
            raise RuntimeError(f"context view message range 插入失败: {view_id}")
        range_index += 1


class RolloutViewMembershipMixin:
    """集中维护 view-local item ordinal，避免各写入入口各自分配顺序。"""

    if TYPE_CHECKING:
        def _view_message_sequences_from_connection(
            self, connection: sqlite3.Connection, view_id: str,
        ) -> list[int]: ...

    @staticmethod
    def _active_context_view_id(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        branch_id: str | None = None,
    ) -> str | None:
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_namespace_state.checkpoint_ns",
            allow_empty=True,
        )
        branch_id = (
            strict_text(branch_id, field="branches.branch_id")
            if branch_id is not None
            else None
        )
        if branch_id is None:
            branch_row = connection.execute(
                "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                (checkpoint_ns,),
            ).fetchone()
            if branch_row is None:
                return None
            branch_id = strict_text(
                branch_row[0],
                field="checkpoint_namespace_state.active_branch_id",
            )
        view_row = connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
            (branch_id,),
        ).fetchone()
        if view_row is None or view_row[0] is None:
            return None
        return strict_text(view_row[0], field="branches.head_view_id")

    def _writable_active_context_view_id(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        branch_id: str | None = None,
    ) -> str:
        """实际写入前隔离 checkpoint 引用的 head，并在同一事务发布 successor。"""
        view_id = self._active_context_view_id(connection, checkpoint_ns, branch_id)
        if view_id is None:
            raise RuntimeError(f"rollout namespace 缺少 active context view: {checkpoint_ns!r}")
        pinned = connection.execute(
            "SELECT 1 FROM checkpoints WHERE view_id = ? UNION ALL "
            "SELECT 1 FROM checkpoint_channels WHERE context_view_id = ? LIMIT 1",
            (view_id, view_id),
        ).fetchone()
        if pinned is None:
            return view_id
        branch = connection.execute(
            "SELECT branch_id FROM branches WHERE head_view_id = ? AND status = 'active'",
            (view_id,),
        ).fetchone()
        if branch is None:
            raise RuntimeError(f"active context view 缺少所属 branch: {view_id}")
        active_branch = strict_text(branch[0], field="branches.branch_id")
        timestamp = utc_now_text()
        successor = self._create_view(
            connection, active_branch, view_id, (), timestamp, view_kind="active"
        )
        published = connection.execute(
            "UPDATE branches SET head_view_id = ?, updated_at = ? "
            "WHERE branch_id = ? AND head_view_id = ? AND status = 'active'",
            (successor, timestamp, active_branch, view_id),
        )
        if published.rowcount != 1:
            raise RuntimeError(f"active successor head 发布失败: {active_branch}/{view_id}")
        return successor

    def _append_context_view_items(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_ns: str,
        item_ids: Iterable[str],
        branch_id: str | None = None,
        require_view: bool = False,
        source_kind: Literal["canonical", "display_only"] = "canonical",
    ) -> bool:
        """把 canonical item 幂等加入 active view，并集中分配 item ordinal。

        ``context_view_items`` 是 view 的派生 membership；canonical item 的
        物理 ``item_sequence`` 只用于本次新成员的稳定候选排序，不能覆盖已
        存在的 view-local ordinal。缺失 catalog 或 ordinal 冲突直接报错，不能
        用 ``INSERT OR IGNORE`` 把索引损坏伪装成成功。
        """
        values = tuple(
            dict.fromkeys(
                strict_text(item_id, field="context_view_items.item_id")
                for item_id in item_ids
            )
        )
        if source_kind not in {"canonical", "display_only"}:
            raise ValueError(f"context view item source_kind 非法: {source_kind!r}")
        if not values:
            return False
        view_id = self._active_context_view_id(connection, checkpoint_ns, branch_id)
        if view_id is None:
            if require_view:
                raise RuntimeError(
                    f"rollout namespace 缺少 active context view: {checkpoint_ns!r}"
                )
            return False
        placeholders = ",".join("?" for _ in values)
        catalog_rows = connection.execute(
            f"SELECT item_id, semantic_kind, turn_id, turn_scope, execution_id "
            f"FROM item_catalog WHERE item_id IN ({placeholders}) "
            "ORDER BY item_sequence, item_id",
            values,
        ).fetchall()
        found = {
            strict_text(row[0], field="item_catalog.item_id") for row in catalog_rows
        }
        missing = tuple(item_id for item_id in values if item_id not in found)
        if missing:
            raise RuntimeError(
                "active view membership 缺少 canonical catalog item: "
                + ",".join(missing)
            )
        pending: list[str] = []
        for item_id, semantic_kind, turn_id, turn_scope, execution_id in catalog_rows:
            item_id = strict_text(item_id, field="item_catalog.item_id")
            semantic_kind = strict_text(
                semantic_kind, field=f"item_catalog.semantic_kind: {item_id}"
            )
            turn_id = strict_optional_text(
                turn_id, field=f"item_catalog.turn_id: {item_id}"
            )
            turn_scope = strict_optional_text(
                turn_scope, field=f"item_catalog.turn_scope: {item_id}"
            )
            if turn_scope not in {
                None,
                "turn_root",
                "turn_member",
                "ambient",
                "pending_next_turn",
            }:
                raise RuntimeError(f"item_catalog.turn_scope 非法: {item_id}")
            execution_id = strict_optional_text(
                execution_id, field=f"item_catalog.execution_id: {item_id}"
            )
            if source_kind == "canonical":
                if turn_scope not in {"turn_root", "turn_member"} and not (
                    semantic_kind == "runtime_notice" and turn_scope == "ambient"
                    and turn_id is None and execution_id is None
                ):
                    continue
            elif (
                execution_id is None
                or turn_id is not None
                or (
                    semantic_kind == "runtime_notice"
                    and turn_scope not in {"ambient", "pending_next_turn"}
                )
                or (
                    semantic_kind != "runtime_notice"
                    and turn_scope is not None
                )
            ):
                raise RuntimeError(
                    "display_only membership 必须引用已绑定 execution 的 Turn-less item: "
                    f"item_id={item_id}, semantic_kind={semantic_kind}, "
                    f"turn_scope={turn_scope}"
                )
            existing = connection.execute(
                "SELECT logical_item_ordinal, visible, source_kind FROM context_view_items "
                "WHERE view_id = ? AND item_id = ?",
                (view_id, item_id),
            ).fetchone()
            if existing is not None:
                strict_non_negative_int(
                    existing[0],
                    field=f"context_view_items.logical_item_ordinal: {item_id}",
                )
                if (
                    existing[1] != 1
                    or strict_text(
                        existing[2], field=f"context_view_items.source_kind: {item_id}"
                    )
                    != source_kind
                ):
                    raise RuntimeError(
                        "context view item 已有不同 membership 身份: "
                        f"view_id={view_id}, item_id={item_id}, source_kind={source_kind}"
                    )
                continue
            pending.append(item_id)
        if not pending:
            return False
        view_id = self._writable_active_context_view_id(
            connection, checkpoint_ns, branch_id
        )
        # 两套坐标共享一次 metadata 聚合，不再为同一次追加扫描两遍 view。
        # context 顺序可按 selection 独立重排；末块的 context 坐标不能代表全局最大值。
        next_ordinal, next_context = connection.execute(
            "SELECT COALESCE(MAX(logical_item_ordinal), -1) + 1, "
            "COALESCE(MAX(context_ordinal), -1) + 1 FROM context_view_items WHERE view_id=?",
            (view_id,),
        ).fetchone()
        next_ordinal = strict_non_negative_int(
            next_ordinal,
            field="context_view_items.logical_item_ordinal",
        )
        append_members(connection, view_id, (
            (item_id, next_ordinal + offset, 1, source_kind, 1, next_context + offset)
            for offset, item_id in enumerate(pending)
        ))
        return True

    def _create_view(
        self,
        connection: sqlite3.Connection,
        branch_id: str,
        parent_view_id: str | None,
        visible_sequences: Sequence[int],
        timestamp: str,
        *,
        view_kind: str = "checkpoint",
        ordered_context_item_ids: Sequence[str] | None = None,
    ) -> str:
        branch_id = strict_text(branch_id, field="context_views.branch_id")
        parent_view_id = strict_optional_text(
            parent_view_id, field="context_views.parent_view_id"
        )
        timestamp = strict_text(timestamp, field="context_views.created_at")
        view_kind = strict_text(view_kind, field="context_views.view_kind")
        view_id = "view-" + uuid4().hex
        sequence_values = tuple(
            dict.fromkeys(
                strict_non_negative_int(value, field="messages.message_sequence")
                for value in visible_sequences
            )
        )
        if any(value == 0 for value in sequence_values):
            raise ValueError("messages.message_sequence 必须为正数")
        parent_sequences: tuple[int, ...] = ()
        if parent_view_id and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
            parent_sequences = tuple(self._view_message_sequences_from_connection(connection, parent_view_id))
        if view_kind == "active" and (parent_view_id is None or sequence_values):
            raise ValueError("active successor 必须严格复制一个既有 parent view")
        if view_kind == "active" or (
            view_kind == "checkpoint_messages" and parent_view_id is not None
            and sequence_values == parent_sequences and ordered_context_item_ids is None
        ):
            # 消息选择未变时与 active successor 共用冻结成员克隆，
            # 不能为 metadata-only checkpoint 重建全部成员并重算旧块摘要。
            header = connection.execute(
                "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, "
                "head_turn_id, head_message_sequence, logical_turn_count, control_sequence, created_at) "
                "SELECT ?, ?, ?, ?, head_turn_id, head_message_sequence, "
                "logical_turn_count, control_sequence, ? FROM context_views WHERE view_id = ?",
                (view_id, branch_id, parent_view_id, view_kind, timestamp, parent_view_id),
            )
            if header.rowcount != 1:
                raise RuntimeError(f"context view 克隆的 parent 不存在: {parent_view_id}")
            clone_members(connection, parent_view_id, view_id)
            connection.execute(
                "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, "
                "user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) "
                "SELECT ?, turn_id, logical_turn_ordinal, user_message_sequence, "
                "final_message_sequence, root_input_item_id, "
                "CASE WHEN ?='active' THEN fork_lineage_json ELSE ? END "
                "FROM context_view_turns WHERE view_id = ?",
                (view_id, view_kind, _json({"source_view_id": parent_view_id, "source_branch_id": branch_id}), parent_view_id),
            )
            if view_kind == "checkpoint_messages":
                _insert_view_ranges(connection, view_id, parent_view_id, sequence_values, view_kind)
            else:
                connection.execute(
                    "INSERT INTO context_view_ranges(view_id, range_index, source_kind, source_view_id, "
                    "start_message_sequence, end_message_sequence, source_start_turn_ordinal, "
                    "source_end_turn_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal, "
                    "range_ordinal, source_start_ordinal, source_end_ordinal, message_start_sequence, "
                    "message_end_sequence, logical_start_ordinal, logical_end_ordinal) "
                    "SELECT ?, range_index, source_kind, source_view_id, start_message_sequence, "
                    "end_message_sequence, source_start_turn_ordinal, source_end_turn_ordinal, "
                    "logical_start_turn_ordinal, logical_end_turn_ordinal, range_ordinal, "
                    "source_start_ordinal, source_end_ordinal, message_start_sequence, "
                    "message_end_sequence, logical_start_ordinal, logical_end_ordinal "
                    "FROM context_view_ranges WHERE view_id = ?",
                    (view_id, parent_view_id),
                )
            insert_view_jumps(connection, view_id)
            return view_id
        materialized_sequences = tuple(dict.fromkeys((*parent_sequences, *sequence_values)))
        message_rows = (
            connection.execute(
                # 一次取回 Turn 与 canonical locator，避免再遍历消息并逐条查 catalog。
                # 按消息聚合仍区分 locator 缺失和重复，不能用任意一条匹配掩盖损坏。
                "SELECT m.message_sequence,m.turn_id,m.message_id,MIN(item.item_id),COUNT(item.item_id) "
                "FROM messages m LEFT JOIN item_catalog item "
                "ON item.jsonl_offset=m.jsonl_offset AND item.jsonl_length=m.jsonl_length "
                "WHERE m.message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ") GROUP BY m.message_sequence ORDER BY m.message_sequence",
                materialized_sequences,
            ).fetchall()
            if materialized_sequences
            else []
        )
        if len(message_rows) != len(materialized_sequences):
            raise RuntimeError(
                f"context view 引用了不存在的 message: view_id={view_id}"
            )
        turn_by_sequence = {
            strict_non_negative_int(sequence, field="messages.message_sequence"): strict_optional_text(
                turn_id, field="messages.turn_id"
            )
            for sequence, turn_id, _message_id, _item_id, _item_count in message_rows
        }
        rows: list[tuple[str, int, int, int | None, str | None]] = []
        sequence_set = set(materialized_sequences)
        last_turn_ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_turn_ordinal), 0) FROM context_view_turns WHERE view_id=?",
                (parent_view_id,),
            ).fetchone()[0],
            field="context_view_turns.logical_turn_ordinal",
        )
        # 完整性只取决于本次选集；每个 Turn 核一次，不能对未完整选入的 Turn 逐消息重扫。
        for value in dict.fromkeys(turn_by_sequence.get(sequence) for sequence in materialized_sequences):
            if value is None:
                continue
            row = connection.execute(
                "SELECT turn.turn_kind,turn.first_message_sequence,turn.last_message_sequence,"
                "turn.user_message_sequence,turn.final_message_sequence,record.root_input_item_id,"
                "parent.logical_turn_ordinal,parent.final_message_sequence "
                "FROM turns turn LEFT JOIN turn_records record ON record.turn_id=turn.turn_id "
                "LEFT JOIN context_view_turns parent ON parent.view_id=? AND parent.turn_id=turn.turn_id "
                "WHERE turn.turn_id=?",
                (parent_view_id, value),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"message Turn projection 缺失: {value}")
            turn_kind = strict_text(row[0], field=f"turns.turn_kind: {value}")
            first_sequence = strict_non_negative_int(
                row[1], field=f"turns.first_message_sequence: {value}"
            )
            last_sequence = strict_non_negative_int(
                row[2], field=f"turns.last_message_sequence: {value}"
            )
            user_sequence = strict_optional_non_negative_int(
                row[3], field=f"turns.user_message_sequence: {value}"
            )
            final_sequence = strict_optional_non_negative_int(
                row[4], field=f"turns.final_message_sequence: {value}"
            )
            if turn_kind != "normal" or user_sequence is None:
                continue
            if (
                first_sequence == 0
                or last_sequence == 0
                or last_sequence < first_sequence
                or final_sequence is not None
                and final_sequence < user_sequence
            ):
                raise RuntimeError(f"Turn message sequence 索引非法: {value}")
            # 完整性相对于冻结来源 view 判断；全局 catalog 还包含其它分支、
            # 被替换版本及 fork 的增量恢复 backing，不能要求它们全部可见。
            # 新出现的 Turn 才使用当前已提交消息；并发序号仍不能按连续范围推断。
            inherited_turn = row[6] is not None
            turn_sequences = {
                strict_non_negative_int(
                    message_row[0], field=f"messages.message_sequence: {value}"
                )
                for message_row in connection.execute(
                    # 固定先按 Turn 定位消息再核 membership；SQLite 若先扫父 view，
                    # 每个 Turn 都会重扫全部父成员，使 checkpoint 创建退化为 O(Turn 数 × 成员数)。
                    "SELECT m.message_sequence FROM messages m "
                    "CROSS JOIN item_catalog i ON i.jsonl_offset=m.jsonl_offset AND i.jsonl_length=m.jsonl_length "
                    "CROSS JOIN context_view_items member ON member.item_id=i.item_id "
                    "WHERE member.view_id=? AND member.visible=1 AND m.turn_id=?"
                    if inherited_turn else
                    "SELECT message_sequence FROM messages WHERE turn_id=?",
                    (parent_view_id, value) if inherited_turn else (value,),
                ).fetchall()
            }
            if not turn_sequences or not turn_sequences.issubset(sequence_set):
                continue
            # 复用同一查询中已校验的 Turn 坐标与 root，不再逐 Turn 回读、重复解码。
            # 前缀 fork 的隐藏 final 仍按父 view 判断；本来没有 final 时无需查父指针。
            if final_sequence is not None and final_sequence not in sequence_set:
                if not inherited_turn or row[7] is not None:
                    raise RuntimeError(f"context view 丢失 canonical final message: {view_id}/{value}")
                final_sequence = None
            # fork 的稀疏 Turn 坐标沿父 view 保留；局部历史刷新不能混入重编号坐标。
            if inherited_turn:
                ordinal = strict_non_negative_int(row[6], field="context_view_turns.logical_turn_ordinal")
            else:
                last_turn_ordinal += 1
                ordinal = last_turn_ordinal
            rows.append((
                value, ordinal, user_sequence, final_sequence,
                strict_text(row[5], field="turn_records.root_input_item_id") if row[5] is not None else None,
            ))
        turn_ids = [row[0] for row in rows]
        head_turn = rows[-1][0] if rows else None
        head_message_sequence = (
            max(materialized_sequences) if materialized_sequences else 0
        )
        view_cursor = connection.execute(
            "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, head_turn_id, head_message_sequence, logical_turn_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                view_id,
                branch_id,
                parent_view_id,
                view_kind,
                head_turn,
                head_message_sequence,
                len(rows),
                timestamp,
            ),
        )
        if view_cursor.rowcount != 1:
            raise RuntimeError(f"context view 插入失败: {view_id}")
        _insert_view_ranges(connection, view_id, parent_view_id, sequence_values, view_kind)
        if parent_view_id:
            insert_view_jumps(connection, view_id)
        for turn_id, ordinal, user_sequence, final_sequence, root_item_id in rows:
            turn_cursor = connection.execute(
                "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    view_id,
                    turn_id,
                    ordinal,
                    user_sequence,
                    final_sequence,
                    root_item_id,
                    _json(
                        {
                            "source_view_id": parent_view_id,
                            "source_branch_id": branch_id,
                        }
                    ),
                ),
            )
            if turn_cursor.rowcount != 1:
                raise RuntimeError(
                    f"context view turn 插入失败: {view_id}/{turn_id}"
                )
        candidate_item_ids: set[str] = set()
        if ordered_context_item_ids is not None:
            candidate_item_ids.update(ordered_context_item_ids)
        for _sequence, _turn_id, message_id, item_id, item_count in message_rows:
            message_id = strict_text(message_id, field="messages.message_id")
            if item_count == 0:
                raise RuntimeError(
                    f"message projection 缺少 canonical item: {message_id}"
                )
            if item_count != 1:
                raise RuntimeError(
                    "message projection 对应多个 canonical item: "
                    f"message_id={message_id}, count={item_count}"
                )
            candidate_item_ids.add(
                strict_text(
                    item_id, field="item_catalog.item_id"
                )
            )
        # provider block item 没有独立的 message_sequence；它们仍属于同一
        # Turn 的 active context view。message mirror 与 block item 必须先合并，
        # 再按 canonical item_sequence 排序，不能把 block item 盲目追加到旧
        # LangChain message 顺序之后；否则 assistant tool_call 会落到 tool
        # result 之后，provider/history projector 得到非法因果顺序。
        if turn_ids and view_kind != "checkpoint_messages":
            placeholders = ",".join("?" for _ in turn_ids)
            block_items = connection.execute(
                "SELECT item.item_id FROM item_catalog item "
                "JOIN context_view_items member ON member.item_id=item.item_id "
                f"WHERE member.view_id=? AND member.visible=1 AND item.turn_id IN ({placeholders}) "
                "ORDER BY member.logical_item_ordinal"
                if parent_view_id is not None else
                f"SELECT item_id FROM item_catalog WHERE turn_id IN ({placeholders}) ORDER BY item_sequence, item_id",
                (parent_view_id, *turn_ids) if parent_view_id is not None else tuple(turn_ids),
            ).fetchall()
            candidate_item_ids.update(
                strict_text(item_id, field="item_catalog.item_id")
                for (item_id,) in block_items
            )
        display_only_item_ids: set[str] = set()
        parent_item_order: list[str] = []
        parent_context_order: list[str] = []
        inherited_members: dict[str, tuple[str, int, int, str, int, int | None]] = {}
        backing_only_ids: set[str] = set()
        if parent_view_id is not None and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
            parent_members = connection.execute(
                "SELECT item_id, logical_item_ordinal, visible, source_kind, context_selected, context_ordinal "
                "FROM context_view_items WHERE view_id = ? ORDER BY logical_item_ordinal",
                (parent_view_id,),
            ).fetchall()
            inherited_members = {row[0]: row for row in parent_members}
            backing_only_ids = {row[0] for row in parent_members if row[2] == 0}
            parent_item_order = [row[0] for row in parent_members]
            parent_context_order = [row[0] for row in sorted(
                (row for row in parent_members if row[2] == 1 and row[4] == 1),
                key=lambda row: strict_non_negative_int(row[5], field="context_view_items.context_ordinal"),
            )]
            candidate_item_ids.update(parent_item_order)
            display_only_item_ids = {
                strict_text(row[0], field="context_view_items.item_id")
                for row in parent_members if row[3] == "display_only"
            }
            candidate_item_ids.update(display_only_item_ids)
        if candidate_item_ids:
            placeholders = ",".join("?" for _ in candidate_item_ids)
            ordered_item_rows = connection.execute(
                "SELECT item_id, semantic_kind, turn_id, turn_scope, execution_id "
                f"FROM item_catalog WHERE item_id IN ({placeholders}) ORDER BY item_sequence, item_id",
                tuple(candidate_item_ids),
            ).fetchall()
            # 派生 view 继承父 view 已确定的逻辑相对顺序；新成员才按本次
            # canonical 物理坐标追加，不能用 item_sequence 重排既有成员。
            item_rows_by_id = {row[0]: row for row in ordered_item_rows}
            inherited_ids = [item_id for item_id in parent_item_order if item_id in item_rows_by_id]
            inherited_set = set(inherited_ids)
            ordered_item_rows = [item_rows_by_id[item_id] for item_id in inherited_ids] + [
                row for row in ordered_item_rows if row[0] not in inherited_set
            ]
            # 复用已算出的继承集合；逐项重建父集合会让默认 context 排序退化为 O(n²)。
            context_order = (
                list(ordered_context_item_ids) if ordered_context_item_ids is not None
                else [item_id for item_id in parent_context_order if item_id in item_rows_by_id]
                + [row[0] for row in ordered_item_rows if row[0] not in inherited_set]
            )
            if len(context_order) != len(set(context_order)) or set(context_order) - set(item_rows_by_id):
                raise ValueError("context selection 含重复或不属于该历史 view 的 item")
            context_order = [item_id for item_id in context_order if item_id not in backing_only_ids]
            context_ordinals = {item_id: ordinal for ordinal, item_id in enumerate(context_order)}
            # 展示坐标跨普通 checkpoint 保持稳定；不可见 backing 也保留原行，
            # 使连续空 checkpoint 不丢失高水位，且不会因 raw message 候选而复活。
            next_logical_item_ordinal = max((row[1] for row in inherited_members.values()), default=-1) + 1
            members = []
            for item_id, semantic_kind, turn_id, turn_scope, execution_id in ordered_item_rows:
                if item_id in backing_only_ids:
                    members.append(inherited_members[item_id])
                    continue
                inherited = inherited_members.get(item_id)
                item_id = strict_text(item_id, field="item_catalog.item_id")
                semantic_kind = strict_text(
                    semantic_kind, field=f"item_catalog.semantic_kind:{item_id}"
                )
                turn_id = strict_optional_text(
                    turn_id, field=f"item_catalog.turn_id:{item_id}"
                )
                turn_scope = strict_optional_text(
                    turn_scope, field=f"item_catalog.turn_scope:{item_id}"
                )
                execution_id = strict_optional_text(
                    execution_id, field=f"item_catalog.execution_id:{item_id}"
                )
                source_kind = (
                    "display_only"
                    if item_id in display_only_item_ids or (
                        turn_id is None
                        and execution_id is not None and turn_scope not in {"turn_root", "turn_member"}
                    )
                    else "canonical"
                )
                if source_kind == "display_only":
                    if (
                        turn_id is not None
                        or execution_id is None
                        or (
                            semantic_kind == "runtime_notice"
                            and turn_scope not in {"ambient", "pending_next_turn"}
                        )
                        or (
                            semantic_kind != "runtime_notice"
                            and turn_scope is not None
                        )
                    ):
                        raise RuntimeError(
                            "父 context view 的 display_only membership 指向非法 runtime item: "
                            f"view_id={parent_view_id}, item_id={item_id}"
                        )
                elif turn_scope not in {"turn_root", "turn_member"} and not (
                    semantic_kind == "runtime_notice" and turn_scope == "ambient"
                    and turn_id is None and execution_id is None
                ):
                    # Turn-less internal checkpoint message 不会因为进入
                    # LangGraph messages channel 而变成 canonical history。
                    continue
                if inherited is None:
                    logical_item_ordinal = next_logical_item_ordinal
                    next_logical_item_ordinal += 1
                else:
                    logical_item_ordinal = inherited[1]
                members.append((item_id, logical_item_ordinal, 1, source_kind, int(item_id in context_ordinals), context_ordinals.get(item_id)))
            replace_members(connection, view_id, members)
        return view_id



__all__ = ["RolloutViewMembershipMixin"]
