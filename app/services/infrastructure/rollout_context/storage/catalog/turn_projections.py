"""v2 catalog 的 Turn projection/page SQL 查询。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from sqlite3 import Connection
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM turn_records AS canonical_turn "
    "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
    "JOIN context_view_items AS membership ON membership.item_id = root.item_id "
    "AND membership.view_id = cvt.view_id "
    "WHERE canonical_turn.turn_id = cvt.turn_id "
    "AND cvt.root_input_item_id = canonical_turn.root_input_item_id "
    "AND root.turn_id = canonical_turn.turn_id "
    "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
    "AND root.status = 'completed' AND membership.visible = 1 "
    "AND membership.source_kind = 'canonical')"
)


def _turn_page_row(row: tuple[object, ...]) -> tuple[str, int, int, int, int]:
    if len(row) != 5:
        raise RuntimeError("Turn projection row 字段数量不一致")
    turn_id = strict_text(row[0], field="context_view_turns.turn_id")
    first_sequence = strict_non_negative_int(
        row[1], field=f"turns.first_message_sequence:{turn_id}"
    )
    last_sequence = strict_non_negative_int(
        row[2], field=f"turns.last_message_sequence:{turn_id}"
    )
    ordinal = strict_non_negative_int(
        row[3], field=f"context_view_turns.logical_turn_ordinal:{turn_id}"
    )
    root_ordinal = strict_non_negative_int(
        row[4], field=f"context_view_items.root_logical_item_ordinal:{turn_id}"
    )
    if first_sequence == 0 or last_sequence == 0 or last_sequence < first_sequence:
        raise RuntimeError(f"Turn projection message range 非法: {turn_id}")
    if ordinal == 0:
        raise RuntimeError(f"Turn projection logical ordinal 非法: {turn_id}")
    return turn_id, first_sequence, last_sequence, ordinal, root_ordinal


def require_context_view_order_integrity(connection: Connection, view_id: str) -> None:
    """拒绝缺失、重复或互相冲突的既有 view membership 坐标。"""
    invalid_coordinate = connection.execute(
        "SELECT item_id FROM context_view_items WHERE view_id = ? "
        "AND (typeof(logical_item_ordinal) != 'integer' OR logical_item_ordinal < 0) "
        "LIMIT 1",
        (view_id,),
    ).fetchone()
    if invalid_coordinate is not None:
        raise RuntimeError(
            "context view projection integrity error: logical_item_ordinal 非法 "
            f"view_id={view_id}, item_id={invalid_coordinate[0]}"
        )

    duplicate_coordinate = connection.execute(
        "SELECT logical_item_ordinal FROM context_view_items WHERE view_id = ? "
        "GROUP BY logical_item_ordinal HAVING COUNT(*) > 1 LIMIT 1",
        (view_id,),
    ).fetchone()
    if duplicate_coordinate is not None:
        raise RuntimeError(
            "context view projection integrity error: logical_item_ordinal 重复 "
            f"view_id={view_id}, ordinal={duplicate_coordinate[0]}"
        )

    unknown_membership = connection.execute(
        "SELECT item_id, source_kind FROM context_view_items WHERE view_id = ? "
        "AND source_kind NOT IN ('canonical', 'display_only') LIMIT 1",
        (view_id,),
    ).fetchone()
    if unknown_membership is not None:
        raise RuntimeError(
            "context view item source_kind 未注册: "
            f"{strict_text(unknown_membership[1], field='context_view_items.source_kind')}"
        )

    missing_membership = connection.execute(
        "SELECT cvt.turn_id FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "LEFT JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE cvt.view_id = ? AND root.turn_id = canonical_turn.turn_id "
        "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
        "AND root.status = 'completed' AND membership.item_id IS NULL LIMIT 1",
        (view_id,),
    ).fetchone()
    if missing_membership is not None:
        raise RuntimeError(
            "context view projection integrity error: Turn root 缺少 view membership "
            f"view_id={view_id}, turn_id={missing_membership[0]}"
        )

    invalid_root_membership = connection.execute(
        "SELECT cvt.turn_id FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE cvt.view_id = ? AND root.turn_id = canonical_turn.turn_id "
        "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
        "AND root.status = 'completed' AND membership.visible = 1 "
        "AND membership.source_kind != 'canonical' LIMIT 1",
        (view_id,),
    ).fetchone()
    if invalid_root_membership is not None:
        raise RuntimeError(
            "context view projection integrity error: 可见 Turn root 不是 canonical member "
            f"view_id={view_id}, turn_id={invalid_root_membership[0]}"
        )

    unordered_roots = connection.execute(
        "SELECT turn_id, logical_turn_ordinal, root_ordinal, previous_root_ordinal FROM ("
        "SELECT cvt.turn_id, cvt.logical_turn_ordinal, membership.logical_item_ordinal AS root_ordinal, "
        "LAG(membership.logical_item_ordinal) OVER (ORDER BY cvt.logical_turn_ordinal) AS previous_root_ordinal "
        "FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE cvt.view_id = ? AND cvt.root_input_item_id = canonical_turn.root_input_item_id "
        "AND root.turn_id = canonical_turn.turn_id AND root.semantic_kind = 'user_input' "
        "AND root.turn_scope = 'turn_root' AND root.status = 'completed' "
        "AND membership.visible = 1 AND membership.source_kind = 'canonical'"
        ") WHERE previous_root_ordinal IS NOT NULL AND root_ordinal <= previous_root_ordinal LIMIT 1",
        (view_id,),
    ).fetchone()
    if unordered_roots is not None:
        raise RuntimeError(
            "context view projection integrity error: Turn root membership 顺序与 Turn ordinal 冲突 "
            f"view_id={view_id}, turn_id={unordered_roots[0]}, "
            f"turn_ordinal={unordered_roots[1]}, root_ordinal={unordered_roots[2]}, "
            f"previous_root_ordinal={unordered_roots[3]}"
        )


class TurnProjectionQueryMixin:
    """读取已提交 Turn projection/cursor，不承担业务 DTO 规则。"""

    def read_context_turn_page(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        direction: str,
        anchor_ordinal: int | None,
        upper_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int, int]], bool]:
        """按逻辑 Turn 序号执行 keyset 分页，并多读一行判断是否还有数据。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        if direction not in {"tail", "before", "head", "after"}:
            raise ValueError(f"不支持的 Turn keyset 方向: {direction}")
        limit = strict_non_negative_int(limit, field="turn_page.limit")
        if limit < 1:
            raise ValueError("Turn keyset limit 必须大于 0")
        anchor_ordinal = strict_optional_non_negative_int(
            anchor_ordinal, field="turn_page.anchor_ordinal"
        )
        upper_ordinal = strict_optional_non_negative_int(
            upper_ordinal, field="turn_page.upper_ordinal"
        )
        if upper_ordinal == 0:
            raise ValueError("turn_page.upper_ordinal 必须大于 0")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        require_context_view_order_integrity(connection, view_id)
        if (
            connection.execute(
                "SELECT 1 FROM context_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            is None
        ):
            raise KeyError(f"context view 不存在: {view_id}")
        if upper_ordinal is None:
            return [], False
        query_limit = limit + 1
        params: list[object] = [view_id]
        where = "cvt.view_id = ? AND cvt.logical_turn_ordinal <= ?"
        params.append(upper_ordinal)
        order = "ASC"
        if direction == "tail":
            order = "DESC"
        elif direction == "before":
            if anchor_ordinal is None:
                raise ValueError("before keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal < ?"
            params.append(anchor_ordinal)
            order = "DESC"
        elif direction == "head":
            order = "ASC"
        elif direction == "after":
            if anchor_ordinal is None:
                raise ValueError("after keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal > ?"
            params.append(anchor_ordinal)
            order = "ASC"
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, "
            "cvt.logical_turn_ordinal, root_membership.logical_item_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            "JOIN context_view_items AS root_membership ON root_membership.view_id = cvt.view_id "
            "AND root_membership.item_id = cvt.root_input_item_id "
            f"WHERE {where} AND {VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal {order} LIMIT ?",
            (*params, query_limit),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        if direction in {"tail", "before"}:
            selected.reverse()
        return [_turn_page_row(row) for row in selected], has_more

    def read_context_turn_window(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        anchor_ordinal: int,
        before: int,
        after: int,
    ) -> list[tuple[str, int, int, int, int]]:
        """只读取游标附近的逻辑 Turn，用于 around 请求。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        anchor_ordinal = strict_non_negative_int(
            anchor_ordinal, field="turn_window.anchor_ordinal"
        )
        before = strict_non_negative_int(before, field="turn_window.before")
        after = strict_non_negative_int(after, field="turn_window.after")
        if anchor_ordinal < 1:
            raise ValueError("around keyset 参数非法")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        require_context_view_order_integrity(connection, view_id)
        rows = connection.execute(
            "SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, "
            "cvt.logical_turn_ordinal, root_membership.logical_item_ordinal "
            "FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            "JOIN context_view_items AS root_membership ON root_membership.view_id = cvt.view_id "
            "AND root_membership.item_id = cvt.root_input_item_id "
            f"WHERE cvt.view_id = ? AND cvt.logical_turn_ordinal BETWEEN ? AND ? AND {VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, max(1, anchor_ordinal - before), anchor_ordinal + after),
        ).fetchall()
        return [_turn_page_row(row) for row in rows]

    def read_context_turn_ids(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        turn_ids: Iterable[str],
    ) -> list[tuple[str, int, int, int, int]]:
        """按指定 Turn ID 读取当前 view 中存在的 Turn，并按逻辑序号排序。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        ids = tuple(
            dict.fromkeys(
                strict_text(turn_id, field="turn_ids.turn_id") for turn_id in turn_ids
            )
        )
        if not ids:
            return []
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        require_context_view_order_integrity(connection, view_id)
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, "
            "cvt.logical_turn_ordinal, root_membership.logical_item_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            "JOIN context_view_items AS root_membership ON root_membership.view_id = cvt.view_id "
            "AND root_membership.item_id = cvt.root_input_item_id "
            f"WHERE cvt.view_id = ? AND cvt.turn_id IN ({placeholders}) AND {VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, *ids),
        ).fetchall()
        return [_turn_page_row(row) for row in rows]

    def _upsert_turn(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        sequence: int,
        message_id: str,
        role: str,
        branch_id: str,
        timestamp: str,
    ) -> None:
        turn_id = strict_text(turn_id, field="turns.turn_id")
        sequence = strict_non_negative_int(sequence, field="turns.message_sequence")
        if sequence == 0:
            raise ValueError(f"turns.message_sequence 必须为正数: {turn_id}")
        message_id = strict_text(message_id, field="turns.message_id")
        role = strict_text(role, field="turns.role")
        branch_id = strict_text(branch_id, field="turns.branch_id")
        timestamp = strict_text(timestamp, field="turns.updated_at")
        row = connection.execute(
            "SELECT first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            if role != "user" or turn_id.startswith("internal-"):
                return
            ordinal = strict_non_negative_int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turns"
                ).fetchone()[0],
                field="turns.turn_ordinal",
            )
            if ordinal == 0:
                raise RuntimeError("turns.turn_ordinal 必须为正数")
            connection.execute(
                "INSERT INTO turns(turn_id, turn_ordinal, turn_kind, branch_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at) VALUES (?, ?, 'normal', ?, ?, ?, ?, NULL, NULL, 'running', ?, ?)",
                (
                    turn_id,
                    ordinal,
                    branch_id,
                    sequence,
                    sequence,
                    sequence,
                    timestamp,
                    timestamp,
                ),
            )
            return
        first_sequence = strict_non_negative_int(
            row[0], field=f"turns.first_message_sequence: {turn_id}"
        )
        last_sequence = strict_non_negative_int(
            row[1], field=f"turns.last_message_sequence: {turn_id}"
        )
        user_sequence_stored = strict_optional_non_negative_int(
            row[2], field=f"turns.user_message_sequence: {turn_id}"
        )
        final_sequence = strict_optional_non_negative_int(
            row[3], field=f"turns.final_message_sequence: {turn_id}"
        )
        final_message_id = strict_optional_text(
            row[4], field=f"turns.final_message_id: {turn_id}"
        )
        if final_sequence is not None and final_message_id is None:
            raise RuntimeError(f"turns final message identity 缺失: {turn_id}")
        if first_sequence == 0 or last_sequence == 0:
            raise RuntimeError(f"turns message sequence 必须为正数: {turn_id}")
        if last_sequence < first_sequence:
            raise RuntimeError(f"turns message sequence 倒序: {turn_id}")
        user_sequence = (
            user_sequence_stored
            if user_sequence_stored is not None
            else sequence
            if role == "user"
            else None
        )
        reopened = (
            final_sequence is not None
            and role in {"assistant", "tool"}
            and sequence > final_sequence
        )
        updated = connection.execute(
            "UPDATE turns SET last_message_sequence = ?, user_message_sequence = COALESCE(user_message_sequence, ?), final_message_sequence = ?, final_message_id = ?, status = ?, updated_at = ? WHERE turn_id = ?",
            (
                sequence,
                user_sequence,
                None if reopened else final_sequence,
                None if reopened else final_message_id,
                "running"
                if reopened
                else "completed"
                if final_sequence is not None
                else "running",
                timestamp,
                turn_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"turns 更新失败: {turn_id}")
