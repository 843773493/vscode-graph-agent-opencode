"""canonical catalog 的 active context view item membership。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from app.core.sqlite_state import utc_now_text
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


class RolloutViewMembershipMixin:
    """集中维护 view-local item ordinal，避免各写入入口各自分配顺序。"""

    if TYPE_CHECKING:
        def _view_message_sequences_from_connection(
            self, connection: sqlite3.Connection, view_id: str, visited: set[str] | None = None,
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
        next_ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_item_ordinal), -1) + 1 "
                "FROM context_view_items WHERE view_id = ?",
                (view_id,),
            ).fetchone()[0],
            field="context_view_items.logical_item_ordinal",
        )
        next_context = connection.execute(
            "SELECT COALESCE(MAX(context_ordinal), -1) + 1 FROM context_view_items WHERE view_id=?",
            (view_id,),
        ).fetchone()[0]
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
        if view_kind == "active":
            if parent_view_id is None or sequence_values:
                raise ValueError("active successor 必须严格复制一个既有 parent view")
            # active writer 的 successor 只复制已冻结的 view-local 事实；
            # 不从全局 Turn/item catalog 重投影，避免夹带 checkpoint 后的新事实。
            header = connection.execute(
                "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, "
                "head_turn_id, head_message_sequence, logical_turn_count, control_sequence, created_at) "
                "SELECT ?, ?, ?, 'active', head_turn_id, head_message_sequence, "
                "logical_turn_count, control_sequence, ? FROM context_views WHERE view_id = ?",
                (view_id, branch_id, parent_view_id, timestamp, parent_view_id),
            )
            if header.rowcount != 1:
                raise RuntimeError(f"active successor parent view 不存在: {parent_view_id}")
            clone_members(connection, parent_view_id, view_id)
            connection.execute(
                "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, "
                "user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) "
                "SELECT ?, turn_id, logical_turn_ordinal, user_message_sequence, "
                "final_message_sequence, root_input_item_id, fork_lineage_json "
                "FROM context_view_turns WHERE view_id = ?",
                (view_id, parent_view_id),
            )
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
            parent_by_view = dict(connection.execute(
                "SELECT view_id, parent_view_id FROM context_views"
            ).fetchall())
            level = 0
            while True:
                ancestor = view_id
                for _ in range(2**level):
                    ancestor = parent_by_view.get(ancestor)
                    if ancestor is None:
                        break
                if ancestor is None:
                    break
                connection.execute(
                    "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) "
                    "VALUES (?, ?, ?, ?)",
                    (view_id, level, ancestor, 2**level),
                )
                level += 1
            return view_id
        materialized_sequences = sequence_values
        if parent_view_id and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
            # 新 checkpoint 的 visible_sequences 可能只是本次 delta；Turn
            # 索引也必须基于完整的父链，否则最新 view 会只有 ToolMessage，
            # 历史分页会暂时看不到这条正在执行的 Turn。
            materialized_sequences = tuple(
                dict.fromkeys(
                    [
                        *self._view_message_sequences_from_connection(
                            connection,
                            parent_view_id,
                        ),
                        *sequence_values,
                    ]
                )
            )
        message_rows = (
            connection.execute(
                "SELECT message_sequence, turn_id FROM messages WHERE message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ")",
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
            for sequence, turn_id in message_rows
        }
        turn_ids: list[str] = []
        sequence_set = set(materialized_sequences)
        for sequence in materialized_sequences:
            value = turn_by_sequence.get(sequence)
            if value is None:
                continue
            if value in turn_ids:
                continue
            row = connection.execute(
                "SELECT turn_kind, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (value,),
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
            inherited_turn = parent_view_id is not None and connection.execute(
                "SELECT 1 FROM context_view_turns WHERE view_id=? AND turn_id=?",
                (parent_view_id, value),
            ).fetchone() is not None
            turn_sequences = {
                strict_non_negative_int(
                    message_row[0], field=f"messages.message_sequence: {value}"
                )
                for message_row in connection.execute(
                    "SELECT m.message_sequence FROM messages m "
                    "JOIN item_catalog i ON i.jsonl_offset=m.jsonl_offset AND i.jsonl_length=m.jsonl_length "
                    "JOIN context_view_items member ON member.item_id=i.item_id "
                    "WHERE member.view_id=? AND member.visible=1 AND m.turn_id=?"
                    if inherited_turn else
                    "SELECT message_sequence FROM messages WHERE turn_id=?",
                    (parent_view_id, value) if inherited_turn else (value,),
                ).fetchall()
            }
            if turn_sequences and turn_sequences.issubset(sequence_set):
                turn_ids.append(value)
        rows: list[tuple[str, int, object, object, str | None]] = []
        for ordinal, turn_id in enumerate(turn_ids, start=1):
            row = connection.execute(
                "SELECT user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is not None:
                user_sequence_value = strict_optional_non_negative_int(
                    row[0], field=f"turns.user_message_sequence: {turn_id}"
                )
                final_sequence_value = strict_optional_non_negative_int(
                    row[1], field=f"turns.final_message_sequence: {turn_id}"
                )
                # Turn 保留来源 final 身份；派生视图只索引本次实际包含的消息，
                # 不能把前缀 fork 隐藏的恢复 backing 重新变成展示指针。
                if final_sequence_value not in sequence_set:
                    parent_turn = connection.execute(
                        "SELECT final_message_sequence FROM context_view_turns WHERE view_id=? AND turn_id=?",
                        (parent_view_id, turn_id),
                    ).fetchone()
                    if final_sequence_value is not None and (parent_turn is None or parent_turn[0] is not None):
                        raise RuntimeError(f"context view 丢失 canonical final message: {view_id}/{turn_id}")
                    final_sequence_value = None
                root_row = connection.execute(
                    "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                rows.append(
                    (
                        turn_id,
                        ordinal,
                        user_sequence_value,
                        final_sequence_value,
                        strict_text(root_row[0], field="turn_records.root_input_item_id")
                        if root_row is not None and root_row[0] is not None
                        else None,
                    )
                )
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
        ranges: list[tuple[int, int]] = []
        for sequence in sequence_values:
            if not ranges or sequence != ranges[-1][1] + 1:
                ranges.append((sequence, sequence))
            else:
                ranges[-1] = (ranges[-1][0], sequence)
        range_index = 0
        if parent_view_id and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
            # 普通 checkpoint 可能只携带本次 LangGraph delta（例如单独的
            # ToolMessage），而不是完整 messages 快照。parent_view_id 仅用于
            # 跳转索引并不能参与消息物化；显式保留父 view，才能让对应的
            # assistant tool call 与后续 ToolMessage 一起进入下一次模型请求。
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
        if parent_view_id:
            jump_cursor = connection.execute(
                "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, 0, ?, 1)",
                (view_id, parent_view_id),
            )
            if jump_cursor.rowcount != 1:
                raise RuntimeError(f"context view ancestor jump 插入失败: {view_id}")
            parent_by_view = {
                strict_text(row[0], field="context_views.view_id"): strict_optional_text(
                    row[1], field="context_views.parent_view_id"
                )
                for row in connection.execute(
                    "SELECT view_id, parent_view_id FROM context_views"
                ).fetchall()
            }
            level = 1
            while True:
                ancestor = view_id
                for _ in range(2**level):
                    ancestor = parent_by_view.get(ancestor)
                    if ancestor is None:
                        break
                if ancestor is None:
                    break
                jump_cursor = connection.execute(
                    "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, ?, ?, ?)",
                    (
                        view_id,
                        level,
                        ancestor,
                        2**level,
                    ),
                )
                if jump_cursor.rowcount != 1:
                    raise RuntimeError(
                        f"context view ancestor jump 插入失败: {view_id}/{level}"
                    )
                level += 1
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
        item_rows = (
            connection.execute(
                "SELECT m.message_sequence, m.message_id, m.jsonl_offset, m.jsonl_length FROM messages AS m WHERE m.message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ") ORDER BY m.message_sequence",
                materialized_sequences,
            ).fetchall()
            if materialized_sequences
            else []
        )
        candidate_item_ids: set[str] = set()
        if ordered_context_item_ids is not None:
            candidate_item_ids.update(ordered_context_item_ids)
        for message_sequence, message_id, jsonl_offset, jsonl_length in item_rows:
            strict_non_negative_int(
                message_sequence, field="messages.message_sequence"
            )
            message_id = strict_text(message_id, field="messages.message_id")
            item_rows_for_message = connection.execute(
                "SELECT item_id FROM item_catalog WHERE jsonl_offset = ? AND jsonl_length = ? "
                "ORDER BY item_sequence",
                (jsonl_offset, jsonl_length),
            ).fetchall()
            if not item_rows_for_message:
                raise RuntimeError(
                    f"message projection 缺少 canonical item: {message_id}"
                )
            if len(item_rows_for_message) != 1:
                raise RuntimeError(
                    "message projection 对应多个 canonical item: "
                    f"message_id={message_id}, count={len(item_rows_for_message)}"
                )
            candidate_item_ids.add(
                strict_text(
                    item_rows_for_message[0][0], field="item_catalog.item_id"
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
        if parent_view_id is not None and view_kind in {"checkpoint", "checkpoint_messages", "compaction"}:
            parent_members = connection.execute(
                "SELECT item_id, source_kind, context_selected, context_ordinal FROM context_view_items "
                "WHERE view_id = ? AND visible = 1 ORDER BY logical_item_ordinal",
                (parent_view_id,),
            ).fetchall()
            parent_item_order = [row[0] for row in parent_members]
            parent_context_order = [row[0] for row in sorted(
                (row for row in parent_members if row[2] == 1),
                key=lambda row: strict_non_negative_int(row[3], field="context_view_items.context_ordinal"),
            )]
            candidate_item_ids.update(parent_item_order)
            display_only_item_ids = {
                strict_text(row[0], field="context_view_items.item_id")
                for row in parent_members if row[1] == "display_only"
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
            context_order = (
                list(ordered_context_item_ids) if ordered_context_item_ids is not None
                else [item_id for item_id in parent_context_order if item_id in item_rows_by_id]
                + [row[0] for row in ordered_item_rows if row[0] not in set(parent_item_order)]
            )
            if len(context_order) != len(set(context_order)) or set(context_order) - set(item_rows_by_id):
                raise ValueError("context selection 含重复或不属于该历史 view 的 item")
            context_ordinals = {item_id: ordinal for ordinal, item_id in enumerate(context_order)}
            logical_item_ordinal = 0
            members = []
            for item_id, semantic_kind, turn_id, turn_scope, execution_id in ordered_item_rows:
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
                members.append((item_id, logical_item_ordinal, 1, source_kind, int(item_id in context_ordinals), context_ordinals.get(item_id)))
                logical_item_ordinal += 1
            replace_members(connection, view_id, members)
        return view_id



__all__ = ["RolloutViewMembershipMixin"]
