"""Turn 窗口定位与 internal execution history 的有界 catalog 读取。"""

from __future__ import annotations

import json
import sqlite3
from itertools import pairwise

from app.core.sqlite_state import load_sql
from app.domain.itemized.identity.item_projection import ItemProjectionManifest
from app.services.infrastructure.rollout_context.execution.terminal_helpers import (
    read_terminal_result,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_response_outline,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _item_elapsed_ms,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projections import (
    VISIBLE_NORMAL_TURN_PREDICATE,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    read_last_visible_ordinal,
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

_CANDIDATES_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/candidates.sql",
)
_POINT_CANDIDATES_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/point_candidates.sql",
)
_CARRIERS_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/carriers.sql",
) + load_sql("app.services.infrastructure.rollout_context.storage", "migrations/history/content_carrier_replacements.sql")
_INVALID_SCOPE_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/invalid_scope.sql",
)
_SURVIVOR_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/survivor.sql",
)

_COORDINATES_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/activity_coordinates.sql",
).format(survivor=_SURVIVOR_SQL)


def _response_coordinates(connection: sqlite3.Connection, view_id: str, execution_id: str,
    snapshot_upper: int, item_ids: tuple[str, ...]) -> dict[str, dict[int, dict[str, object]]]:
    """只在 SQLite 派生真实段首与组首，不加载正文、不以当前页首作为身份。"""
    candidates = """WITH page_candidates AS MATERIALIZED (
        SELECT refs.view_id,membership.logical_item_ordinal,item.item_id,item.projection_manifest_json
        FROM item_catalog item
        CROSS JOIN context_membership_items membership INDEXED BY membership_items_identity ON membership.item_id=item.item_id
        CROSS JOIN context_view_item_blocks refs ON refs.block_id=membership.block_id AND refs.view_id=:view_id
        WHERE item.execution_id=:execution_id AND item.turn_id IS NULL
            AND membership.visible=1
            AND membership.source_kind='display_only' AND membership.logical_item_ordinal<=:snapshot_upper
    )"""
    rows = connection.execute(candidates + _CARRIERS_SQL + _COORDINATES_SQL, {
        "view_id":view_id,"execution_id":execution_id,"snapshot_upper":snapshot_upper,
        "coordinate_items":json.dumps(item_ids),
    })
    result: dict[str, dict[int, dict[str, object]]] = {}
    for item_id, ordinal, segment, group, group_part, kind in rows:
        coordinates = {"process_segment_item_id":segment}
        if kind in {"reasoning","reasoning_summary","reasoning_encrypted","tool_call","tool_result"}:
            coordinates.update(activity_group_item_id=group, activity_group_part_ordinal=group_part)
        result.setdefault(item_id, {})[ordinal] = coordinates
    return result


class InternalExecutionHistoryMixin:
    """只从同一 snapshot 的 view membership 与 execution admission 读取历史。"""

    def read_item_position(self, snapshot: RolloutReadSnapshot, view_id: str, item_id: str) -> tuple[int, str | None]:
        """按单个 Item locator 核可见成员，再找同 view 的最近 Turn root。"""
        connection = self._snapshot_connection(snapshot)
        row = connection.execute("SELECT membership.logical_item_ordinal,item.turn_id FROM context_view_items membership JOIN item_catalog item ON item.item_id=membership.item_id WHERE membership.view_id=? AND membership.item_id=? AND membership.visible=1", (view_id, item_id)).fetchone()
        if row is None:
            raise KeyError(f"当前 view 中没有 Item: {item_id}")
        if row[1] is not None:
            return row[0], row[1]
        turn = connection.execute(
            "SELECT cvt.turn_id FROM context_view_turns cvt "
            "JOIN context_view_items root ON root.view_id=cvt.view_id AND root.item_id=cvt.root_input_item_id "
            "WHERE cvt.view_id=? AND root.logical_item_ordinal<=? "
            f"AND {VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY root.logical_item_ordinal DESC LIMIT 1",
            (view_id, row[0]),
        ).fetchone()
        return row[0], turn[0] if turn else None

    def read_history_snapshot_bounds(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> HistorySnapshotBounds:
        """读取同一 view/snapshot 的两条分页 lane 共用 logical item 坐标上界。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        connection = self._snapshot_connection(snapshot)
        if (
            connection.execute(
                "SELECT 1 FROM context_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            is None
        ):
            raise KeyError(f"context view 不存在: {view_id}")
        turn_row = connection.execute(
            "SELECT MAX(cvt.logical_turn_ordinal) "
            "FROM context_view_turns AS cvt "
            f"WHERE cvt.view_id = ? AND {VISIBLE_NORMAL_TURN_PREDICATE}",
            (view_id,),
        ).fetchone()
        item_ordinal = read_last_visible_ordinal(connection, view_id)
        if turn_row is None:
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
                item_ordinal, field="context_view_items.upper_logical_item_ordinal"
            )
            if item_ordinal is not None
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
        snapshot_upper_logical_item_ordinal: int | None,
        execution_id: str | None = None,
        item_ids: tuple[str, ...] | None = None,
        outline: bool = False,
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
        snapshot_upper = strict_optional_non_negative_int(
            snapshot_upper_logical_item_ordinal, field="snapshot_upper_logical_item_ordinal",
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
        if upper_logical_item_ordinal is None or snapshot_upper is None:
            return InternalExecutionHistoryPage(entries=(), has_more=False)

        if connection.execute(
            "SELECT 1 FROM executions WHERE turn_id IS NULL AND execution_origin='internal' "
            + ("AND execution_id=? " if execution_id is not None else "") + "LIMIT 1",
            (execution_id,) if execution_id is not None else (),
        ).fetchone() is None:
            if item_ids is not None:
                raise ValueError("internal execution item 不属于当前可见历史或已被替代")
            return InternalExecutionHistoryPage(entries=(), has_more=False)

        # 先定位本页候选，只按这些 item 的身份读取必要替代载体。
        # 替代载体仍冻结于整个 view/execution/snapshot，不能被页 anchor 截断。
        parameters: dict[str, object] = {
            "view_id": view_id, "snapshot_upper": snapshot_upper,
            "execution_id": strict_text(execution_id, field="execution_id") if execution_id is not None else None,
            "upper": min(upper_logical_item_ordinal, snapshot_upper),
            "candidate_limit": limit + 1,
        }
        predicates = []
        if item_ids is not None:
            if execution_id is None or not 1 <= len(item_ids) <= 4 or len(set(item_ids)) != len(item_ids):
                raise ValueError("internal item 定点读取要求同一 execution 的 1 到 4 个唯一 ID")
            predicates.append("item.item_id IN (" + ",".join(f":item_{index}" for index in range(len(item_ids))) + ")")
            parameters.update({f"item_{index}": strict_text(value, field="item_id") for index, value in enumerate(item_ids)})
        if lower_logical_item_ordinal is not None:
            predicates.append("membership.logical_item_ordinal >= :lower")
            parameters["lower"] = lower_logical_item_ordinal
        operator = "<" if direction == "before" else ">"
        order = "DESC" if direction == "before" else "ASC"
        rows = []
        anchor = anchor_logical_item_ordinal
        while len(rows) <= limit:
            page_predicates = list(predicates)
            if anchor is not None:
                page_predicates.append(f"membership.logical_item_ordinal {operator} :anchor")
                parameters["anchor"] = anchor
            carriers = ((_POINT_CANDIDATES_SQL if item_ids is not None else _CANDIDATES_SQL) + _CARRIERS_SQL).format(
                page_predicates="".join(f" AND {predicate}" for predicate in page_predicates), order=order,
            )
            candidates = connection.execute(
                carriers +
                "SELECT item.item_sequence,item.item_id,execution.execution_id,execution.job_id,"
                "execution.outcome,execution.created_at,execution.updated_at,membership.logical_item_ordinal,"
                "execution.created_at,execution.session_id,execution.thread_id,"
                + _SURVIVOR_SQL + ", (" + _INVALID_SCOPE_SQL.rstrip().rstrip(";") + "), membership.block_ordinal "
                "FROM page_candidates AS membership "
                "JOIN item_catalog AS item ON item.item_id=membership.item_id "
                "JOIN executions AS execution ON execution.execution_id=item.execution_id "
                f"ORDER BY membership.block_ordinal {order},membership.logical_item_ordinal {order}",
                parameters,
            ).fetchall()
            if any(row[12] is not None for row in candidates):
                raise ValueError("最终 content part carrier 缺少唯一 typed model-call scope")
            rows.extend((*row[:11], row[13]) for row in candidates if row[11])
            if len(candidates) < limit + 1 or item_ids is not None:
                break
            anchor = candidates[-1][7]
        rows = rows[:limit + 1]
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
        if item_ids is not None and {row[1] for row in selected} != set(item_ids):
            raise ValueError("internal execution item 不属于当前可见历史或已被替代")
        if direction == "before":
            selected.reverse()
        # keyset 只消费本页坐标；全 view 的缺失、重复及 source_kind 由完整校验负责。
        ordinals = [strict_non_negative_int(row[7], field="context_view_items.logical_item_ordinal") for row in selected]
        if any(current <= previous for previous, current in pairwise(ordinals)):
            raise RuntimeError(f"context view projection integrity error: logical_item_ordinal 重复: view_id={view_id}")
        item_ids = tuple(
            strict_text(row[1], field="item_catalog.item_id")
            for row in selected
        )
        metadata_by_id = {}
        if outline:
            for row in selected:
                metadata = connection.execute(
                    "SELECT item.semantic_kind,item.payload_kind,item.status,item.created_at,item.projection_manifest_json,"
                    "item.tool_call_id,CASE WHEN item.semantic_kind IN ('assistant_output','tool_call') THEN ip.content ELSE '' END, "
                    "item.message_group_id FROM item_catalog item JOIN item_projections ip ON ip.item_id=item.item_id "
                    "WHERE item.item_id=?",
                    (row[1],),
                ).fetchone()
                if metadata is None:
                    raise RuntimeError("internal 活动缺少索引元数据")
                metadata_by_id[row[1]] = metadata
        # notice 是独立展示入口，不属于可隐藏的思考／工具正文。
        body_ids = tuple(item_id for item_id in item_ids
            if not outline or metadata_by_id[item_id][0] == "runtime_notice")
        items = self.read_items(snapshot.session_id, snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns, item_ids=body_ids, snapshot=snapshot) if body_ids else ()
        items_by_id = {item.item_id: item for item in items}
        if len(items_by_id) != len(body_ids):
            raise RuntimeError("internal history page item locator 不完整")
        # elapsed 的前驱是同 execution 的前一 visible/display_only 成员，
        # 包括已被替代的载体；只为本页返回项点读，不对全历史做 LAG。
        previous_times = {}
        for row in selected:
            previous = connection.execute(
                "SELECT item.created_at FROM context_view_item_blocks AS refs "
                "CROSS JOIN context_membership_items AS member ON member.block_id=refs.block_id "
                "CROSS JOIN item_catalog AS item ON item.item_id=member.item_id "
                "WHERE refs.view_id=? AND member.visible=1 AND member.source_kind='display_only' "
                "AND item.execution_id=? AND item.turn_id IS NULL AND refs.block_ordinal<=? AND member.logical_item_ordinal<? "
                "ORDER BY refs.block_ordinal DESC,member.logical_item_ordinal DESC LIMIT 1",
                (view_id, row[2], row[11], row[7]),
            ).fetchone()
            previous_times[row[1]] = previous[0] if previous is not None else row[8]
        activities: dict[str, dict[str, object]] = {}
        if outline:
            for row in selected:
                metadata = metadata_by_id[row[1]]
                if metadata[0] == "runtime_notice":
                    continue
                manifest = ItemProjectionManifest.from_dict(json.loads(metadata[4]))
                identity = manifest.projection_identity
                kind = "text" if metadata[0] == "assistant_output" else (
                    "reasoning_summary" if metadata[0] == "reasoning" and metadata[1] == "summary" else
                    "reasoning_encrypted" if metadata[0] == "reasoning" and metadata[1] in {"opaque", "extension"} else metadata[0])
                common = {"item_id": row[1], "item_sequence": row[0], "logical_item_ordinal": row[7],
                    "created_at": metadata[3], "status": metadata[2], "model_call_id": identity.model_call_id if identity else None}
                # 消息身份不代表结构化载体；独立 reasoning 没有 messages 投影行。
                if metadata[7] is not None or metadata[0] == "assistant_output" and metadata[1] == "structured_content":
                    outline_parts = read_response_outline(connection, row[1])
                    members = [{**common, **part} for part in outline_parts]
                else:
                    members = [{**common, "kind": kind,
                        "part_ordinal": identity.block_index if identity and identity.block_index is not None else 0,
                        "content_block_index": identity.block_index if identity else None,
                        "tool_call_id": metadata[5], "tool_name": metadata[6] if kind == "tool_call" else None,
                        "text": metadata[6] if kind == "text" else "", "truncated": False}]
                activities[row[1]] = {"activity_items": members}
        coordinates = {}
        for coordinate_execution in dict.fromkeys(row[2] for row in selected):
            coordinates.update(_response_coordinates(connection, view_id, coordinate_execution, snapshot_upper,
                tuple(row[1] for row in selected if row[2] == coordinate_execution)))
        terminal_results = {
            execution_id: read_terminal_result(connection, execution_id=execution_id, turn_id=None)
            for execution_id in dict.fromkeys(row[2] for row in selected)
        }
        entries = tuple(
            InternalExecutionHistoryEntry(
                execution_id=strict_text(
                    row[2], field="executions.execution_id"
                ),
                job_id=strict_text(row[3], field="executions.job_id"),
                session_id=strict_text(row[9], field="executions.session_id"),
                thread_id=strict_text(row[10], field="executions.thread_id"),
                outcome=strict_text(row[4], field="executions.outcome"),
                failure=terminal_results[row[2]].failure if terminal_results[row[2]] is not None else None,
                terminal_commit_id=terminal_results[row[2]].commit_id if terminal_results[row[2]] is not None else None,
                created_at=strict_text(row[5], field="executions.created_at"),
                updated_at=strict_text(row[6], field="executions.updated_at"),
                logical_item_ordinal=strict_non_negative_int(
                    row[7], field="context_view_items.logical_item_ordinal"
                ),
                # notice 是执行的输入锚点，可能早于准入；它自身不是计时活动。
                elapsed_ms=0 if row[1] in items_by_id and items_by_id[row[1]].semantic_kind == "runtime_notice" else _item_elapsed_ms(
                    metadata_by_id[row[1]][3] if outline else items_by_id[row[1]].created_at, previous_times[row[1]]),
                item=items_by_id.get(row[1]), activity=activities.get(row[1]), coordinates=coordinates.get(row[1]),
            )
            for row in selected
        )
        return InternalExecutionHistoryPage(entries=entries, has_more=has_more)


__all__ = ["InternalExecutionHistoryMixin"]
