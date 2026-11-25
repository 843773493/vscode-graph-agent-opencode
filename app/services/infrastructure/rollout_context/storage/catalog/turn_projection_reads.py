"""v2 catalog 的 Turn projection 读取与 canonical activity 归并。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.sqlite_state import load_sql
from app.services.infrastructure.rollout_context.execution.terminal_helpers import (
    read_terminal_result,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.tool_result_identity import (
    read_tool_result_summaries,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_activity_merge import (
    _finalize_projections,
    _merge_activity_items,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _required_datetime,
)
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


_ACTIVITY_COORDINATES_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/turn_activity_coordinates.sql",
).format(
    replacements=load_sql(
        "app.services.infrastructure.rollout_context.storage", "migrations/history/content_carrier_replacements.sql",
    ),
    survivor=load_sql(
        "app.services.infrastructure.rollout_context.storage", "migrations/history/survivor.sql",
    ),
)
_TOOL_COORDINATES_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history/tool_call_coordinates.sql",
)


_V2_TO_HISTORY_STATUS = {
    "open": "accepted",
    "active": "running",
    "completed": "completed",
    "completed_empty": "completed",
    "interrupted": "timed_out",
    "cancelled": "cancelled",
    "failed": "failed",
    "unknown": "failed",
}


class TurnProjectionReadMixin:
    def read_existing_turn_ids(
        self, snapshot: RolloutReadSnapshot, turn_ids: Iterable[str]
    ) -> set[str]:
        """只查 canonical Turn 存在性，供已离开当前 view 的引用分类。"""
        ids = tuple(dict.fromkeys(strict_text(value, field="turn_ids.turn_id") for value in turn_ids))
        if not ids:
            return set()
        connection = self._snapshot_connection(snapshot)
        placeholders = ",".join("?" for _ in ids)
        return {
            strict_text(row[0], field="turn_records.turn_id")
            for row in connection.execute(
                f"SELECT turn_id FROM turn_records WHERE turn_id IN ({placeholders})",
                ids,
            )
        }

    def read_turn_projections(
        self, snapshot: RolloutReadSnapshot, view_id: str, turn_ids: Iterable[str], *,
        activity_before: int | None = None,
        activity_limit: int | None = None, item_ids: tuple[str, ...] | None = None,
        outline: bool = False, include: frozenset[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        view_id = strict_text(view_id, field="context_views.view_id")
        ids = tuple(
            dict.fromkeys(
                strict_text(turn_id, field="turn_ids.turn_id") for turn_id in turn_ids
            )
        )
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        connection = self._snapshot_connection(snapshot)
        if connection.execute("SELECT 1 FROM context_views WHERE view_id = ?", (view_id,)).fetchone() is None:
            raise KeyError(f"context view 不存在: {view_id}")
        turns = connection.execute(
            "SELECT t.turn_id, t.first_message_sequence, t.last_message_sequence, "
            "t.final_message_sequence, t.final_message_id, t.status, t.created_at, t.updated_at, "
            "root.created_at FROM turns t LEFT JOIN messages root "
            "ON root.message_sequence=t.user_message_sequence AND root.turn_id=t.turn_id "
            f"AND root.role='user' WHERE t.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        v2_turns = connection.execute(
            "SELECT tr.turn_id, tr.status, tr.final_item_id, tr.session_id, "
            "tr.thread_id, tr.last_execution_id, e.execution_id, e.session_id, "
            "e.thread_id, e.turn_id, e.job_id, tr.created_at, tr.updated_at FROM turn_records AS tr "
            "LEFT JOIN executions AS e ON e.execution_id = tr.last_execution_id "
            f"WHERE tr.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        final_rows = connection.execute(
            # 先按本页 final 定位成员，再做 LEFT JOIN；直接连接多表 view
            # 会物化整个父 view，连没有 final 的进行中 Turn 也会扫描旧历史。
            "WITH final_membership AS MATERIALIZED ("
            "SELECT member.item_id,member.logical_item_ordinal,member.visible,member.source_kind "
            "FROM turns t JOIN turn_records record ON record.turn_id=t.turn_id "
            "CROSS JOIN item_catalog item ON item.item_id=record.final_item_id "
            "CROSS JOIN context_view_items member ON member.item_id=item.item_id "
            f"WHERE t.turn_id IN ({placeholders}) AND member.view_id=?) "
            "SELECT t.turn_id, t.final_message_sequence, m.message_id, m.turn_id, "
            "item.item_id, item.turn_id, item.semantic_kind, item.status, "
            "membership.visible, membership.source_kind, "
            "selected_turn.turn_id, selected_turn.final_message_sequence, "
            "EXISTS(SELECT 1 FROM message_projections canonical_projection "
            "WHERE canonical_projection.message_sequence = t.final_message_sequence) "
            "FROM turns t "
            "LEFT JOIN turn_records record ON record.turn_id=t.turn_id "
            "LEFT JOIN messages m ON m.message_sequence = t.final_message_sequence "
            "LEFT JOIN item_catalog item ON item.item_id=record.final_item_id "
            "LEFT JOIN context_view_turns selected_turn ON selected_turn.turn_id = t.turn_id "
            "AND selected_turn.view_id = ? "
            "LEFT JOIN final_membership AS membership ON membership.item_id = item.item_id "
            f"WHERE t.turn_id IN ({placeholders})",
            (*ids, view_id, view_id, *ids),
        ).fetchall()
        # 相同轻索引一次产生整轮统计与本页坐标；分页及正文 include 不重算标题计数。
        activity_stats: dict[str, dict[str, object]] = {}
        coordinates = []
        page_limit = min(activity_limit if activity_limit is not None else 256, 256)
        for row in connection.execute(_ACTIVITY_COORDINATES_SQL, {
            "view_id": view_id, "turn_ids": json.dumps(ids),
            "item_ids": json.dumps(item_ids) if item_ids is not None else None,
            "before_item": activity_before,
            "item_limit": page_limit + 1,
            "include_final": include is None or bool(include & {"final_response", "assistant", "activity_outline"}),
        }):
            if row[0] is None:
                activity_stats[row[5]] = {
                    "duration_ms": None,
                    "item_count": strict_non_negative_int(row[6], field="activity_stats.item_count"),
                    "first_item_sequence": strict_optional_non_negative_int(row[7], field="activity_stats.first_item_sequence"),
                    "last_item_sequence": strict_optional_non_negative_int(row[8], field="activity_stats.last_item_sequence"),
                }
            else:
                coordinates.append((*row[:6], row[9]))
        has_more = len(coordinates) > page_limit
        coordinates = coordinates[:page_limit]
        selected_ids = tuple(dict.fromkeys(row[0] for row in coordinates))
        # 从本页真实调用身份定点关联，组尾是否在页内不影响其它成员；
        # 配对坐标不得携带当前view已排除的回执。
        tool_rows = connection.execute(_TOOL_COORDINATES_SQL, {
            "view_id": view_id, "item_ids": json.dumps(selected_ids),
        }).fetchall() if selected_ids else ()
        activity_filter = " AND ic.item_id IN (" + ",".join("?" for _ in selected_ids) + ")" if selected_ids else " AND 0"
        activity_parameters = selected_ids
        activity_tail = " ORDER BY ic.turn_id, membership.logical_item_ordinal"
        # 只读取请求需要的有界正文；工具调用名称始终是轻元数据。
        content_kinds = {"tool_call"}
        if include is None or include & {"assistant", "assistant_text", "text", "final_response", "activity_outline"}:
            content_kinds.add("assistant_output")
        if not outline and (include is None or include & {"thinking", "reasoning_detail", "reasoning_summary"}):
            content_kinds.update({"reasoning", "compaction_summary"})
        read_reasoning = not outline and (include is None or bool(include & {"thinking", "reasoning_detail"}))
        read_summary = not outline and (include is None or bool(include & {"thinking", "reasoning_detail", "reasoning_summary"}))
        reasoning_content = "COALESCE(rb.reasoning_text,rb.summary_text,'')" if read_reasoning else (
            "COALESCE(rb.summary_text,'')" if read_summary else "''"
        )
        reasoning_truncated = "CASE WHEN rb.reasoning_text IS NOT NULL THEN rb.reasoning_truncated ELSE rb.summary_truncated END" if read_reasoning else (
            "rb.summary_truncated" if read_summary else "0"
        )
        content_sql = "CASE WHEN ic.semantic_kind='reasoning' THEN " + reasoning_content + " WHEN ic.semantic_kind IN (" + ",".join("'" + kind + "'" for kind in sorted(content_kinds)) + ") THEN ip.content ELSE '' END, "
        activity_rows = connection.execute(
            "SELECT ic.turn_id, ic.item_sequence, ic.item_id, ic.semantic_kind, "
            "ic.payload_kind, ic.status, ic.created_at, ic.producer_ref_json, "
            "ic.projection_manifest_json, "
            + content_sql +
            "CASE WHEN ic.semantic_kind='reasoning' THEN " + reasoning_truncated + " ELSE ip.content_truncated END, ic.tool_call_id, "
            "membership.logical_item_ordinal, ic.message_group_id, item_message.message_sequence, rb.kind "
            # 先按本页 Turn 定位 activity，再查可见成员，避免每次翻页扫描整个 view。
            "FROM item_catalog AS ic CROSS JOIN item_projections AS ip "
            "ON ip.item_id = ic.item_id AND ip.item_sequence = ic.item_sequence "
            "CROSS JOIN context_view_items AS membership ON membership.item_id = ic.item_id "
            "AND membership.view_id = ? AND membership.visible = 1 "
            "AND membership.source_kind = 'canonical' "
            "LEFT JOIN messages item_message ON item_message.jsonl_offset=ic.jsonl_offset "
            "AND item_message.jsonl_length=ic.jsonl_length "
            "LEFT JOIN reasoning_blocks rb ON rb.item_id=ic.item_id "
            f"WHERE ic.turn_id IN ({placeholders}) AND ic.semantic_kind IN "
            "('reasoning','tool_call','tool_result','compaction_summary','assistant_output') "
            + activity_filter + activity_tail,
            (view_id, *ids, *activity_parameters),
        )
        activity_rows = list(activity_rows)
        result: dict[str, dict[str, object]] = {}
        for row in turns:
            if len(row) != 9:
                raise RuntimeError("Turn projection source row 字段数量不一致")
            turn_id = strict_text(row[0], field="turns.turn_id")
            first_sequence = strict_non_negative_int(
                row[1], field=f"turns.first_message_sequence:{turn_id}"
            )
            last_sequence = strict_non_negative_int(
                row[2], field=f"turns.last_message_sequence:{turn_id}"
            )
            final_sequence = strict_optional_non_negative_int(
                row[3], field=f"turns.final_message_sequence:{turn_id}"
            )
            if (
                first_sequence == 0
                or last_sequence == 0
                or last_sequence < first_sequence
            ):
                raise RuntimeError(f"Turn projection message range 非法: {turn_id}")
            final_message_id = strict_optional_text(
                row[4], field=f"turns.final_message_id:{turn_id}"
            )
            status = strict_text(row[5], field=f"turns.status:{turn_id}")
            created_at = strict_text(row[6], field=f"turns.created_at:{turn_id}")
            updated_at = strict_text(row[7], field=f"turns.updated_at:{turn_id}")
            # root 即便退出当前 visible 集合，消息时间仍来自同一冻结索引。
            user_created_at = strict_text(row[8], field=f"messages.user_created_at:{turn_id}")
            result[turn_id] = {
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
                "final_message_sequence": final_sequence,
                "final_message_id": final_message_id,
                "status": status,
                "final_source": "turn_finalize" if final_sequence is not None else None,
                "final_response_text": "",
                "final_response_text_truncated": False,
                "final_items": [],
                "thinking_blocks": [],
                "assistant_text_sequences": [],
                "has_encrypted_reasoning": False,
                "tool_items": [],
                "activity_items": [],
                "created_at": created_at,
                "user_created_at": user_created_at,
                "updated_at": updated_at,
                "activity_stats": activity_stats.get(turn_id, {
                    "duration_ms": None,
                    "item_count": 0,
                    "first_item_sequence": None,
                    "last_item_sequence": None,
                }),
            }
        # TurnRecord 是 v2 的生命周期事实源；旧 turns 行仅保留 message
        # sequence projection，不能覆盖失败、取消或执行丢失等 v2 状态。
        projected_turn_ids: set[str] = set()
        validated_historical_owners: set[tuple[str, str]] = set()
        # turn_records 保存记录自身的执行归属，可能等于当前归属，也可能是由本地冻结 binding 保留的来源归属；只在当前库校验，不重定向源路径。
        current_storage_owner = (snapshot.session_id, snapshot.thread_id)
        for (
            turn_id, status, final_item_id, historical_session_id, historical_thread_id,
            last_execution_id, execution_id, execution_session_id,
            execution_thread_id, execution_turn_id, job_id, accepted_at, changed_at,
        ) in v2_turns:
            turn_id = strict_text(turn_id, field="turn_records.turn_id")
            historical_owner = (
                strict_text(
                    historical_session_id,
                    field=f"turn_records.session_id:{turn_id}",
                ),
                strict_text(
                    historical_thread_id,
                    field=f"turn_records.thread_id:{turn_id}",
                ),
            )
            last_execution_id = strict_text(
                last_execution_id, field=f"turn_records.last_execution_id:{turn_id}"
            )
            if (
                execution_id != last_execution_id
                or (execution_session_id, execution_thread_id) != historical_owner
                or execution_turn_id != turn_id
            ):
                raise RuntimeError(
                    "Turn 与 last execution owner/identity 不一致: "
                    f"turn_id={turn_id}, last_execution_id={last_execution_id}, "
                    f"turn_owner={historical_owner}, execution="
                    f"{(execution_id, execution_session_id, execution_thread_id, execution_turn_id)}"
                )
            if historical_owner not in validated_historical_owners:
                require_historical_scope(
                    connection, *historical_owner,
                    current_owner=current_storage_owner,
                )
                validated_historical_owners.add(historical_owner)
            job_id = strict_text(job_id, field=f"executions.job_id:{last_execution_id}")
            if job_id == turn_id:
                raise RuntimeError(f"Turn 与 last execution Job 身份不得别名: {turn_id}")
            status = strict_text(status, field=f"turn_records.status:{turn_id}")
            if status not in _V2_TO_HISTORY_STATUS:
                raise RuntimeError(f"未知 v2 Turn status: {status}")
            final_item_id = strict_optional_text(
                final_item_id, field=f"turn_records.final_item_id:{turn_id}"
            )
            projection = result.get(turn_id)
            if projection is None:
                raise RuntimeError(f"TurnRecord 缺少 turns projection: {turn_id}")
            terminal = read_terminal_result(connection, execution_id=last_execution_id, turn_id=turn_id)
            if terminal is not None and terminal.failure is not None:
                projection["failure"] = terminal.failure
            projection["job_id"] = job_id
            projection["status"] = _V2_TO_HISTORY_STATUS[status]
            # 消息时钟不代表 Turn 生命周期；未终结时不生成完成耗时。
            accepted = _required_datetime(accepted_at, field="turn_records.created_at")
            changed = _required_datetime(changed_at, field="turn_records.updated_at")
            if changed < accepted:
                raise RuntimeError(f"Turn 生命周期时间倒退: {turn_id}")
            projection["activity_stats"]["duration_ms"] = (
                None if status in {"open", "active"} else (changed - accepted) // timedelta(milliseconds=1)
            )
            projected_turn_ids.add(turn_id)
            if final_item_id is not None:
                projection["final_item_id"] = final_item_id
        if projected_turn_ids != set(result):
            raise RuntimeError(
                f"turns projection 缺少 canonical TurnRecord: {set(result) - projected_turn_ids}"
            )
        for (
            turn_id, final_sequence, message_id, message_turn_id,
            item_id, item_turn_id, semantic_kind, item_status,
            final_visible, final_source_kind,
            selected_turn_id, selected_final_sequence, has_canonical_projection,
        ) in final_rows:
            turn_id = strict_text(turn_id, field="turns.turn_id")
            final_sequence = strict_optional_non_negative_int(
                final_sequence, field=f"turns.final_message_sequence:{turn_id}"
            )
            selected_final_sequence = strict_optional_non_negative_int(
                selected_final_sequence,
                field=f"context_view_turns.final_message_sequence:{view_id}/{turn_id}",
            )
            if selected_turn_id != turn_id:
                raise RuntimeError(f"Turn 缺少所选 view projection: {view_id}/{turn_id}")
            if final_sequence is None:
                if result[turn_id].get("final_item_id") is not None or selected_final_sequence is not None:
                    raise RuntimeError(f"canonical final item 缺少 message projection: {turn_id}")
                continue
            projection = result[turn_id]
            if (
                item_id is None
                or item_id != projection.get("final_item_id")
                or message_id != projection["final_message_id"]
                or item_turn_id != turn_id
                or message_turn_id != turn_id
                or semantic_kind != "assistant_output"
                or item_status != "completed"
                or projection["status"] != "completed"
            ):
                raise RuntimeError(f"final projection 与 canonical Turn/item 指针不一致: {turn_id}")
            if has_canonical_projection != 1:
                raise RuntimeError(f"canonical final item 缺少 message projection: {turn_id}")
            if selected_final_sequence is None:
                if final_visible == 1:
                    raise RuntimeError(f"所选 view final 指针与可见 membership 不一致: {view_id}/{item_id}")
                # 历史 final 身份保留；展示指针只指向所选 view，禁止后续补载 backing。
                projection["final_message_sequence"] = None
                projection["final_message_id"] = None
                projection["final_source"] = None
                continue
            if selected_final_sequence != final_sequence:
                raise RuntimeError(f"所选 view final 指针与 canonical Turn 不一致: {view_id}/{turn_id}")
            if final_visible != 1 or final_source_kind != "canonical":
                raise RuntimeError(f"canonical final item 缺少可见 view membership: {view_id}/{item_id}")
        _merge_activity_items(result, tool_rows, activity_rows)
        tool_results = read_tool_result_summaries(connection, view_id, selected_ids)
        # 坐标来自相同轻索引，正文补读和任意分页均不重新猜组首或从页首计时。
        for turn_id, projection in result.items():
            selected = {row[0]: row for row in coordinates if row[5] == turn_id}
            members = []
            for member in projection["activity_items"]:
                coordinate = selected[member["item_id"]]
                if coordinate[6]:
                    if member["status"] != "completed":
                        raise RuntimeError(f"final输出成员不是completed: {member['item_id']}")
                    # final 与过程共用同一选集和续点；只保留本页有界派生文本。
                    remaining = max(0, 65536 - len(projection["final_response_text"]))
                    clipped = len(member["text"]) > remaining
                    member["text"] = member["text"][:remaining]
                    member["truncated"] |= clipped
                    member["elapsed_ms"] = 0
                    projection["final_items"].append(member)
                    projection["final_response_text"] += member["text"]
                    projection["final_response_text_truncated"] |= member["truncated"]
                    continue
                member["previous_created_at"] = coordinate[3] or projection["created_at"]
                if member["kind"] in {"reasoning", "reasoning_summary", "reasoning_encrypted", "tool_call", "tool_result"}:
                    member["activity_group_item_id"] = coordinate[4]
                # 摘要与正文是同一 Item 的两种安全视图，不再生成第二个内容身份。
                if read_summary and not read_reasoning and member["kind"] == "reasoning" and member["text"]:
                    member["kind"] = "reasoning_summary"
                tool_result = tool_results.get(member["item_id"])
                if tool_result is not None:
                    member["tool_outcome"] = tool_result.tool_outcome
                    if tool_result.tool_execution is not None:
                        member["tool_execution"] = tool_result.tool_execution.to_dict()
                members.append(member)
            projection["activity_items"] = members
            projection["activity_has_more"] = has_more
            if has_more and selected:
                projection["activity_continuation"] = min(row[2] for row in selected.values())
        _finalize_projections(result)
        return result
