"""Turn 窗口定位与 internal execution history 的有界 catalog 读取。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.records import parse_item_projection_manifest
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _item_elapsed_ms,
)
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


@dataclass(frozen=True, slots=True)
class InternalContentCarrier:
    """同一历史快照内的 typed carrier 镜像，不加载 canonical 正文。"""

    item_id: str
    semantic_kind: str
    projection_identity: ItemProjectionIdentity | None
    provenance_relations: tuple[ItemProvenanceRelation, ...]


_INTERNAL_MEMBER_PREDICATES = (
    "membership.view_id = ?",
    "membership.visible = 1",
    "membership.source_kind = 'display_only'",
    "item.execution_id = execution.execution_id",
    "item.turn_id IS NULL",
    "execution.execution_origin = 'internal'",
    "execution.turn_id IS NULL",
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
                "AND membership.item_id = root.item_id "
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
                    "Turn item window endpoint 缺少 canonical user root: "
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

    def _internal_history_scope(
        self, snapshot: RolloutReadSnapshot, view_id: str,
    ) -> tuple[sqlite3.Connection, str, str]:
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        if connection.execute(
            "SELECT 1 FROM context_views WHERE view_id = ?", (view_id,),
        ).fetchone() is None:
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
        return connection, session_id, thread_id

    def read_internal_content_carriers(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        upper_logical_item_ordinal: int | None,
        execution_id: str | None = None,
    ) -> tuple[InternalContentCarrier, ...]:
        """只读冻结上界内的完整 carrier 身份，刻意不施加页锚点或窗口下界。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        upper = strict_optional_non_negative_int(
            upper_logical_item_ordinal, field="upper_logical_item_ordinal",
        )
        connection, _session_id, _thread_id = self._internal_history_scope(snapshot, view_id)
        if upper is None:
            return ()
        predicates = [*_INTERNAL_MEMBER_PREDICATES, "membership.logical_item_ordinal <= ?"]
        parameters: list[object] = [view_id, upper]
        if execution_id is not None:
            predicates.append("execution.execution_id = ?")
            parameters.append(strict_text(execution_id, field="execution_id"))
        rows = connection.execute(
            "SELECT item.item_id, item.semantic_kind, item.projection_manifest_json "
            "FROM context_view_items AS membership "
            "JOIN item_catalog AS item ON item.item_id = membership.item_id "
            "JOIN executions AS execution ON execution.execution_id = item.execution_id "
            "WHERE " + " AND ".join(predicates), parameters,
        ).fetchall()
        carriers: list[InternalContentCarrier] = []
        for item_id, semantic_kind, manifest_json in rows:
            manifest = parse_item_projection_manifest(json.loads(strict_text(
                manifest_json, field="item_catalog.projection_manifest_json",
            )))
            carriers.append(InternalContentCarrier(
                item_id=strict_text(item_id, field="item_catalog.item_id"),
                semantic_kind=strict_text(semantic_kind, field="item_catalog.semantic_kind"),
                projection_identity=(
                    ItemProjectionIdentity.from_dict(manifest["projection_identity"])
                    if manifest["projection_identity"] is not None else None
                ),
                provenance_relations=tuple(
                    ItemProvenanceRelation.from_dict(relation)
                    for relation in manifest["provenance_relations"]
                ),
            ))
        return tuple(carriers)

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
        selected_item_ids: frozenset[str],
        execution_id: str | None = None,
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
        connection, session_id, thread_id = self._internal_history_scope(snapshot, view_id)
        if (
            lower_logical_item_ordinal is not None
            and upper_logical_item_ordinal is not None
            and lower_logical_item_ordinal > upper_logical_item_ordinal
        ):
            return InternalExecutionHistoryPage(entries=(), has_more=False)
        if upper_logical_item_ordinal is None:
            return InternalExecutionHistoryPage(entries=(), has_more=False)

        predicates = list(_INTERNAL_MEMBER_PREDICATES)
        parameters: list[object] = [view_id]
        if execution_id is not None:
            predicates.append("execution.execution_id = ?")
            parameters.append(strict_text(execution_id, field="execution_id"))
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
        # elapsed 沿同 view、同 execution 的前一 visible/display_only 逻辑成员，
        # 即使前项不在本页也沿用它的原时间；没有前项才以 execution.created_at 起算。
        rows = connection.execute(
            "WITH selected(item_id) AS MATERIALIZED (SELECT value FROM json_each(?)) "
            "SELECT item.item_sequence, item.item_id, execution.execution_id, "
            "execution.job_id, execution.outcome, execution.created_at, "
            "execution.updated_at, membership.logical_item_ordinal, "
            "COALESCE((SELECT previous.created_at FROM context_view_items AS previous_member "
            "JOIN item_catalog AS previous ON previous.item_id = previous_member.item_id "
            "WHERE previous_member.view_id = membership.view_id "
            "AND previous_member.visible = 1 AND previous_member.source_kind = 'display_only' "
            "AND previous.execution_id = execution.execution_id "
            "AND previous_member.logical_item_ordinal < membership.logical_item_ordinal "
            "ORDER BY previous_member.logical_item_ordinal DESC LIMIT 1), execution.created_at), "
            "execution.session_id, execution.thread_id "
            "FROM context_view_items AS membership "
            "JOIN item_catalog AS item ON item.item_id = membership.item_id "
            "JOIN selected ON selected.item_id = item.item_id "
            "JOIN executions AS execution "
            "ON execution.execution_id = item.execution_id "
            "WHERE "
            + " AND ".join(predicates)
            + f" ORDER BY membership.logical_item_ordinal {order} LIMIT ?",
            (json.dumps(sorted(selected_item_ids)), *parameters, limit + 1),
        ).fetchall()
        validated_owners: set[tuple[str, str]] = set()
        for row in rows:
            execution_owner = (
                strict_text(row[9], field="executions.session_id"),
                strict_text(row[10], field="executions.thread_id"),
            )
            if execution_owner not in validated_owners:
                require_historical_scope(
                    connection, *execution_owner,
                    current_owner=(session_id, thread_id),
                )
                validated_owners.add(execution_owner)
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
                session_id=strict_text(row[9], field="executions.session_id"),
                thread_id=strict_text(row[10], field="executions.thread_id"),
                outcome=strict_text(row[4], field="executions.outcome"),
                created_at=strict_text(row[5], field="executions.created_at"),
                updated_at=strict_text(row[6], field="executions.updated_at"),
                logical_item_ordinal=strict_non_negative_int(
                    row[7], field="context_view_items.logical_item_ordinal"
                ),
                elapsed_ms=_item_elapsed_ms(
                    items_by_id[strict_text(row[1], field="item_catalog.item_id")].created_at,
                    row[8],
                ),
                item=items_by_id[
                    strict_text(row[1], field="item_catalog.item_id")
                ],
            )
            for row in selected
        )
        return InternalExecutionHistoryPage(entries=entries, has_more=has_more)


__all__ = ["InternalExecutionHistoryMixin"]
