"""v2 catalog 的 Turn projection/page SQL 查询。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
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
    "AND root.status = 'completed' AND membership.visible = 1)"
)


def _turn_page_row(row: tuple[object, ...]) -> tuple[str, int, int, int]:
    if len(row) != 4:
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
    if first_sequence == 0 or last_sequence == 0 or last_sequence < first_sequence:
        raise RuntimeError(f"Turn projection message range 非法: {turn_id}")
    return turn_id, first_sequence, last_sequence, ordinal


class TurnProjectionQueryMixin:
    """读取已提交 Turn projection/cursor，不承担业务 DTO 规则。"""

    def read_context_turn_page(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        direction: str,
        anchor_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int]], bool]:
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
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        query_limit = limit + 1
        params: list[object] = [view_id]
        where = "cvt.view_id = ?"
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
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
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
    ) -> list[tuple[str, int, int, int]]:
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
        rows = connection.execute(
            "SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            "FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
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
    ) -> list[tuple[str, int, int, int]]:
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
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE cvt.view_id = ? AND cvt.turn_id IN ({placeholders}) AND {VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, *ids),
        ).fetchall()
        return [_turn_page_row(row) for row in rows]
