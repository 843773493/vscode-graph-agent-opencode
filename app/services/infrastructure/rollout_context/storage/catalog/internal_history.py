"""Turn 窗口定位与 internal execution history 的有界 catalog 读取。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.storage.catalog.turn_projections import (
    VISIBLE_NORMAL_TURN_PREDICATE,
    require_context_view_order_integrity,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    ContextTurnItemWindow,
    HistorySnapshotBounds,
    InternalExecutionHistoryEntry,
    InternalExecutionHistoryPage,
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_text,
)


class InternalExecutionHistoryMixin:
    """只从同一 snapshot 的 view membership 与 execution admission 读取历史。"""

    def read_history_snapshot_bounds(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> HistorySnapshotBounds:
        """读取同一 view/snapshot 的两条分页 lane 共用 logical item 坐标上界。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        if (
            connection.execute(
                "SELECT 1 FROM context_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            is None
        ):
            raise KeyError(f"context view 不存在: {view_id}")
        require_context_view_order_integrity(connection, view_id)
        turn_row = connection.execute(
            "SELECT MAX(cvt.logical_turn_ordinal) "
            "FROM context_view_turns AS cvt "
            f"WHERE cvt.view_id = ? AND {VISIBLE_NORMAL_TURN_PREDICATE}",
            (view_id,),
        ).fetchone()
        item_row = connection.execute(
            "SELECT MAX(logical_item_ordinal) "
            "FROM context_view_items "
            "WHERE view_id = ? AND visible = 1",
            (view_id,),
        ).fetchone()
        if turn_row is None or item_row is None:
            raise RuntimeError("history snapshot bounds aggregate 查询没有结果")
        upper_turn_ordinal = (
            strict_non_negative_int(
                turn_row[0], field="context_view_turns.upper_logical_turn_ordinal"
            )
            if turn_row[0] is not None
            else None
        )
        upper_logical_item_ordinal = (
            strict_non_negative_int(
                item_row[0], field="context_view_items.upper_logical_item_ordinal"
            )
            if item_row[0] is not None
            else None
        )
        if upper_turn_ordinal == 0:
            raise RuntimeError("history snapshot turn bound 必须为正数")
        history_view_revision = strict_non_negative_int(
            snapshot.manifest.history_view_revision,
            field="rollout_manifest.history_view_revision",
        )
        return HistorySnapshotBounds(
            view_id=view_id,
            history_view_revision=history_view_revision,
            upper_turn_ordinal=upper_turn_ordinal,
            upper_logical_item_ordinal=upper_logical_item_ordinal,
        )

    def resolve_context_turn_item_window(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        first_logical_turn_ordinal: int,
        last_logical_turn_ordinal: int,
    ) -> ContextTurnItemWindow:
        """把 Turn ordinal window 映射成 canonical membership ordinal bounds。

        不使用 messages 的 sequence，也不通过物理邻接补 membership。缺失 view
        或 endpoint 时保留 exists 标志并返回 null bounds，供 around 游标区分
        空窗口与有效边界。若投影声称 endpoint 存在但 root 不是 canonical 成员，
        则作为 catalog 损坏明确报错。
        """
        view_id = strict_text(view_id, field="context_views.view_id")
        first_logical_turn_ordinal = strict_non_negative_int(
            first_logical_turn_ordinal,
            field="first_logical_turn_ordinal",
        )
        last_logical_turn_ordinal = strict_non_negative_int(
            last_logical_turn_ordinal,
            field="last_logical_turn_ordinal",
        )
        if first_logical_turn_ordinal == 0 or last_logical_turn_ordinal == 0:
            raise ValueError("logical_turn_ordinal 必须大于 0")
        if first_logical_turn_ordinal > last_logical_turn_ordinal:
            raise ValueError("Turn item window 起始 ordinal 不得大于结束 ordinal")

        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        view_exists = (
            connection.execute(
                "SELECT 1 FROM context_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            is not None
        )
        first_turn_exists = False
        last_turn_exists = False
        lower_logical_item_ordinal: int | None = None
        upper_logical_item_ordinal: int | None = None
        if view_exists:
            noncanonical_endpoint_rows = connection.execute(
                "SELECT cvt.logical_turn_ordinal FROM context_view_turns AS cvt "
                "JOIN turn_records AS canonical_turn "
                "ON canonical_turn.turn_id = cvt.turn_id "
                "AND canonical_turn.root_input_item_id = cvt.root_input_item_id "
                "JOIN item_catalog AS root "
                "ON root.item_id = canonical_turn.root_input_item_id "
                "WHERE cvt.view_id = ? AND cvt.logical_turn_ordinal IN (?, ?) "
                "AND root.turn_id = canonical_turn.turn_id "
                "AND root.semantic_kind = 'user_input' "
                "AND root.turn_scope = 'turn_root' AND root.status = 'completed' "
                "AND EXISTS (SELECT 1 FROM context_view_items AS membership "
                "WHERE membership.view_id = cvt.view_id "
                "AND membership.item_id = root.item_id AND membership.visible = 1 "
                "AND membership.source_kind != 'canonical')",
                (
                    view_id,
                    first_logical_turn_ordinal,
                    last_logical_turn_ordinal,
                ),
            ).fetchall()
            noncanonical_endpoint_ordinals = sorted({
                strict_non_negative_int(
                    row[0],
                    field="context_view_turns.logical_turn_ordinal",
                )
                for row in noncanonical_endpoint_rows
            })
            endpoint_rows = connection.execute(
                "SELECT cvt.logical_turn_ordinal FROM context_view_turns AS cvt "
                f"WHERE cvt.view_id = ? AND cvt.logical_turn_ordinal IN (?, ?) "
                f"AND {VISIBLE_NORMAL_TURN_PREDICATE}",
                (
                    view_id,
                    first_logical_turn_ordinal,
                    last_logical_turn_ordinal,
                ),
            ).fetchall()
            endpoint_ordinals = {
                strict_non_negative_int(
                    row[0],
                    field="context_view_turns.logical_turn_ordinal",
                )
                for row in endpoint_rows
            }
            if noncanonical_endpoint_ordinals:
                raise RuntimeError(
                    "Turn item window endpoint 缺少 canonical 可见 user root: "
                    f"view_id={view_id}, ordinals={noncanonical_endpoint_ordinals}"
                )
            first_turn_exists = first_logical_turn_ordinal in endpoint_ordinals
            last_turn_exists = last_logical_turn_ordinal in endpoint_ordinals
            if first_turn_exists and last_turn_exists:
                bounds = connection.execute(
                    "SELECT MIN(membership.logical_item_ordinal), "
                    "MAX(membership.logical_item_ordinal) "
                    "FROM context_view_turns AS cvt "
                    "JOIN item_catalog AS item ON item.turn_id = cvt.turn_id "
                    "JOIN context_view_items AS membership "
                    "ON membership.view_id = cvt.view_id "
                    "AND membership.item_id = item.item_id "
                    "WHERE cvt.view_id = ? "
                    "AND cvt.logical_turn_ordinal BETWEEN ? AND ? "
                    f"AND {VISIBLE_NORMAL_TURN_PREDICATE} "
                    "AND item.turn_scope IN ('turn_root', 'turn_member') "
                    "AND membership.visible = 1 "
                    "AND membership.source_kind = 'canonical'",
                    (
                        view_id,
                        first_logical_turn_ordinal,
                        last_logical_turn_ordinal,
                    ),
                ).fetchone()
                if bounds is None:
                    raise RuntimeError("Turn item window bounds 查询没有结果")
                lower_logical_item_ordinal = (
                    strict_non_negative_int(
                        bounds[0],
                        field="context_view_items.logical_item_ordinal.lower",
                    )
                    if bounds[0] is not None
                    else None
                )
                upper_logical_item_ordinal = (
                    strict_non_negative_int(
                        bounds[1],
                        field="context_view_items.logical_item_ordinal.upper",
                    )
                    if bounds[1] is not None
                    else None
                )
                if (
                    lower_logical_item_ordinal is None
                    or upper_logical_item_ordinal is None
                ):
                    raise RuntimeError(
                        "Turn item window endpoints 是 canonical Turn，但没有 canonical item bound: "
                        f"view_id={view_id}, first={first_logical_turn_ordinal}, "
                        f"last={last_logical_turn_ordinal}"
                    )
                if lower_logical_item_ordinal > upper_logical_item_ordinal:
                    raise RuntimeError("Turn item window canonical membership bounds 顺序非法")

        return ContextTurnItemWindow(
            view_id=view_id,
            first_logical_turn_ordinal=first_logical_turn_ordinal,
            last_logical_turn_ordinal=last_logical_turn_ordinal,
            view_exists=view_exists,
            first_turn_exists=first_turn_exists,
            last_turn_exists=last_turn_exists,
            lower_logical_item_ordinal=lower_logical_item_ordinal,
            upper_logical_item_ordinal=upper_logical_item_ordinal,
        )

    def read_internal_execution_history_page(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        direction: str,
        anchor_logical_item_ordinal: int | None,
        lower_logical_item_ordinal: int | None,
        upper_logical_item_ordinal: int | None,
        limit: int,
    ) -> InternalExecutionHistoryPage:
        """按显式 display-only membership ordinal 做有界 keyset page。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        if direction not in {"before", "after"}:
            raise ValueError("internal history direction 必须为 before 或 after")
        anchor_logical_item_ordinal = strict_optional_non_negative_int(
            anchor_logical_item_ordinal,
            field="anchor_logical_item_ordinal",
        )
        lower_logical_item_ordinal = strict_optional_non_negative_int(
            lower_logical_item_ordinal,
            field="lower_logical_item_ordinal",
        )
        upper_logical_item_ordinal = strict_optional_non_negative_int(
            upper_logical_item_ordinal,
            field="upper_logical_item_ordinal",
        )
        limit = strict_non_negative_int(limit, field="internal_history.limit")
        if limit < 1 or limit > 256:
            raise ValueError("internal history limit 必须在 1 到 256 之间")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        view = connection.execute(
            "SELECT 1 FROM context_views WHERE view_id = ?",
            (view_id,),
        ).fetchone()
        if view is None:
            raise KeyError(f"context view 不存在: {view_id}")
        owner = connection.execute(
            "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if owner is None:
            raise RuntimeError("database_meta 缺少 rollout owner")
        session_id = strict_text(owner[0], field="database_meta.session_id")
        thread_id = strict_text(owner[1], field="database_meta.thread_id")
        if session_id != snapshot.session_id or thread_id != snapshot.thread_id:
            raise RuntimeError(
                "internal history snapshot owner 不匹配: "
                f"snapshot=({snapshot.session_id}, {snapshot.thread_id}), "
                f"database=({session_id}, {thread_id})"
            )
        if (
            lower_logical_item_ordinal is not None
            and upper_logical_item_ordinal is not None
            and lower_logical_item_ordinal > upper_logical_item_ordinal
        ):
            return InternalExecutionHistoryPage(entries=(), has_more=False)
        if upper_logical_item_ordinal is None:
            return InternalExecutionHistoryPage(entries=(), has_more=False)

        predicates = [
            "membership.view_id = ?",
            "membership.visible = 1",
            "membership.source_kind = 'display_only'",
            "item.execution_id = execution.execution_id",
            "item.turn_id IS NULL",
            "execution.session_id = ?",
            "execution.thread_id = ?",
            "execution.execution_origin = 'internal'",
            "execution.turn_id IS NULL",
        ]
        parameters: list[object] = [view_id, session_id, thread_id]
        if lower_logical_item_ordinal is not None:
            predicates.append("membership.logical_item_ordinal >= ?")
            parameters.append(lower_logical_item_ordinal)
        predicates.append("membership.logical_item_ordinal <= ?")
        parameters.append(upper_logical_item_ordinal)
        if anchor_logical_item_ordinal is not None:
            operator = "<" if direction == "before" else ">"
            predicates.append(f"membership.logical_item_ordinal {operator} ?")
            parameters.append(anchor_logical_item_ordinal)
        order = "DESC" if direction == "before" else "ASC"
        rows = connection.execute(
            "SELECT item.item_sequence, item.item_id, execution.execution_id, "
            "execution.job_id, execution.outcome, execution.created_at, "
            "execution.updated_at, membership.logical_item_ordinal "
            "FROM context_view_items AS membership "
            "JOIN item_catalog AS item ON item.item_id = membership.item_id "
            "JOIN executions AS execution "
            "ON execution.execution_id = item.execution_id "
            "WHERE "
            + " AND ".join(predicates)
            + f" ORDER BY membership.logical_item_ordinal {order} LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        if direction == "before":
            selected.reverse()
        item_ids = tuple(
            strict_text(row[1], field="item_catalog.item_id")
            for row in selected
        )
        items = self.read_items(
            snapshot.session_id,
            snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            item_ids=item_ids,
            snapshot=snapshot,
        )
        items_by_id = {item.item_id: item for item in items}
        if len(items_by_id) != len(item_ids):
            raise RuntimeError("internal history page item locator 不完整")
        entries = tuple(
            InternalExecutionHistoryEntry(
                execution_id=strict_text(
                    row[2], field="executions.execution_id"
                ),
                job_id=strict_text(row[3], field="executions.job_id"),
                outcome=strict_text(row[4], field="executions.outcome"),
                created_at=strict_text(row[5], field="executions.created_at"),
                updated_at=strict_text(row[6], field="executions.updated_at"),
                logical_item_ordinal=strict_non_negative_int(
                    row[7], field="context_view_items.logical_item_ordinal"
                ),
                item=items_by_id[
                    strict_text(row[1], field="item_catalog.item_id")
                ],
            )
            for row in selected
        )
        return InternalExecutionHistoryPage(entries=entries, has_more=has_more)


__all__ = ["InternalExecutionHistoryMixin"]
