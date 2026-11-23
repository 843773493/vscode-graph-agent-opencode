"""v2 catalog 的 Turn projection 读取与 canonical activity 归并。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.core.sqlite_state import load_sql
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.execution.terminal_helpers import (
    read_terminal_result,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_response_outline,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_activity_merge import (
    _finalize_projections,
    _merge_activity_items,
    _merge_final_reasoning,
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
        activity_before: tuple[int, int] | None = None,
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
            "e.thread_id, e.turn_id, e.job_id FROM turn_records AS tr "
            "LEFT JOIN executions AS e ON e.execution_id = tr.last_execution_id "
            f"WHERE tr.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        tool_rows = connection.execute(
            "SELECT m.turn_id, ic.item_id, tc.tool_call_id, tc.tool_name, tc.status, "
            "tc.result_message_sequence, tc.assistant_message_sequence, tc.call_index, "
            # 从本页 Turn 的消息定位工具，不能先扫描整个 view 再筛 Turn。
            "ic.projection_manifest_json FROM messages m "
            "CROSS JOIN tool_calls tc ON tc.assistant_message_sequence = m.message_sequence "
            "CROSS JOIN item_catalog ic ON ic.jsonl_offset = m.jsonl_offset "
            "AND ic.jsonl_length = m.jsonl_length "
            "CROSS JOIN context_view_items membership ON membership.item_id = ic.item_id "
            "AND membership.view_id = ? AND membership.visible = 1 "
            "AND membership.source_kind = 'canonical' "
            f"WHERE m.turn_id IN ({placeholders}) "
            "ORDER BY tc.assistant_message_sequence, tc.call_index",
            (view_id, *ids),
        ).fetchall()
        final_rows = connection.execute(
            # 先按本页 final 定位成员，再做 LEFT JOIN；直接连接多表 view
            # 会物化整个父 view，连没有 final 的进行中 Turn 也会扫描旧历史。
            "WITH final_membership AS MATERIALIZED ("
            "SELECT member.item_id,member.logical_item_ordinal,member.visible,member.source_kind "
            "FROM turns t CROSS JOIN messages m ON m.message_sequence=t.final_message_sequence "
            "CROSS JOIN item_catalog item ON item.jsonl_offset=m.jsonl_offset "
            "AND item.jsonl_length=m.jsonl_length "
            "CROSS JOIN context_view_items member ON member.item_id=item.item_id "
            f"WHERE t.turn_id IN ({placeholders}) AND member.view_id=?) "
            "SELECT t.turn_id, t.final_message_sequence, mp.visible_text, "
            "mp.visible_text_truncated, m.message_id, m.turn_id, "
            "item.item_id, item.turn_id, item.semantic_kind, item.status, "
            "item.item_sequence, item.created_at, membership.logical_item_ordinal, "
            "membership.visible, membership.source_kind, "
            "selected_turn.turn_id, selected_turn.final_message_sequence, "
            "EXISTS(SELECT 1 FROM message_projections canonical_projection "
            "WHERE canonical_projection.message_sequence = t.final_message_sequence), "
            "item.projection_manifest_json "
            "FROM turns t "
            "LEFT JOIN messages m ON m.message_sequence = t.final_message_sequence "
            "LEFT JOIN item_catalog item ON item.jsonl_offset = m.jsonl_offset "
            "AND item.jsonl_length = m.jsonl_length "
            "LEFT JOIN context_view_turns selected_turn ON selected_turn.turn_id = t.turn_id "
            "AND selected_turn.view_id = ? "
            "LEFT JOIN final_membership AS membership ON membership.item_id = item.item_id "
            "LEFT JOIN message_projections mp ON mp.message_sequence = t.final_message_sequence "
            "AND selected_turn.final_message_sequence = t.final_message_sequence "
            "AND membership.visible = 1 AND membership.source_kind = 'canonical' "
            f"WHERE t.turn_id IN ({placeholders})",
            (*ids, view_id, view_id, *ids),
        ).fetchall()
        # 在整个选中过程的轻索引中择一，再截页；正文只读取本页真正存活的 Item。
        coordinates = connection.execute(_ACTIVITY_COORDINATES_SQL, {
            "view_id": view_id, "turn_ids": json.dumps(ids),
            "item_ids": json.dumps(item_ids) if item_ids is not None else None,
            "before_item": activity_before[0] if activity_before else None,
            "before_part": activity_before[1] if activity_before else None,
            "part_limit": activity_limit + 1 if activity_limit is not None else -1,
        }).fetchall()
        has_more = activity_limit is not None and len(coordinates) > activity_limit
        coordinates = coordinates[:activity_limit] if activity_limit is not None else coordinates
        selected_ids = tuple(dict.fromkeys(row[0] for row in coordinates))
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
        content_sql = "CASE " + ("WHEN ic.semantic_kind='reasoning' AND ic.payload_kind!='summary' THEN '' " if not read_reasoning else "") + "WHEN ic.semantic_kind IN (" + ",".join("'" + kind + "'" for kind in sorted(content_kinds)) + ") THEN ip.content ELSE '' END, "
        activity_rows = connection.execute(
            "SELECT ic.turn_id, ic.item_sequence, ic.item_id, ic.semantic_kind, "
            "ic.payload_kind, ic.status, ic.created_at, ic.producer_ref_json, "
            "ic.projection_manifest_json, "
            + content_sql +
            "ip.content_truncated, ic.tool_call_id, "
            "membership.logical_item_ordinal "
            # 先按本页 Turn 定位 activity，再查可见成员，避免每次翻页扫描整个 view。
            "FROM item_catalog AS ic CROSS JOIN item_projections AS ip "
            "ON ip.item_id = ic.item_id AND ip.item_sequence = ic.item_sequence "
            "CROSS JOIN context_view_items AS membership ON membership.item_id = ic.item_id "
            "AND membership.view_id = ? AND membership.visible = 1 "
            "AND membership.source_kind = 'canonical' "
            "LEFT JOIN turn_records AS tr ON tr.turn_id = ic.turn_id "
            f"WHERE ic.turn_id IN ({placeholders}) AND (ic.semantic_kind IN "
            "('reasoning','tool_call','tool_result','compaction_summary') "
            "OR (ic.semantic_kind = 'assistant_output' AND ic.item_id IS NOT tr.final_item_id)) "
            + activity_filter + activity_tail,
            (view_id, *ids, *activity_parameters),
        )
        activity_rows = sorted(activity_rows, key=lambda row: (row[0], row[1]))
        final_reasoning_rows = connection.execute(
            "SELECT t.turn_id, rb.message_sequence, rb.content_block_index, "
            "rb.item_index, rb.carrier_type, "
            + ("CASE WHEN json_extract(part.value,'$.kind')='reasoning' THEN rb.reasoning_text END, " if read_reasoning else
               "CASE WHEN json_extract(part.value,'$.kind')='reasoning' THEN '' END, ")
            + ("CASE WHEN json_extract(part.value,'$.kind')='reasoning_summary' THEN rb.summary_text END, " if read_summary else
               "CASE WHEN json_extract(part.value,'$.kind')='reasoning_summary' THEN '' END, ") +
            "rb.signature_present, CASE WHEN json_extract(part.value,'$.kind')='reasoning_encrypted' THEN rb.encrypted_length END, "
            "rb.item_id, ic.item_id, ic.item_sequence, ic.created_at, membership.logical_item_ordinal, "
            "json_extract(part.value,'$.part_ordinal') "
            "FROM turns t JOIN turn_records tr ON tr.turn_id=t.turn_id "
            "JOIN item_catalog ic ON ic.item_id=tr.final_item_id "
            "JOIN context_view_items membership ON membership.item_id=ic.item_id AND membership.view_id=? "
            "AND membership.visible=1 AND membership.source_kind='canonical' "
            "JOIN message_projections mp ON mp.message_sequence=t.final_message_sequence "
            "JOIN json_each((SELECT value FROM json_each(mp.response_outline_json) WHERE key=ic.item_id)) part "
            "JOIN reasoning_blocks rb ON rb.message_sequence=t.final_message_sequence "
            "AND rb.content_block_index=json_extract(part.value,'$.content_block_index') "
            "AND rb.item_index=json_extract(part.value,'$.item_index') "
            f"WHERE t.turn_id IN ({placeholders}) AND json_extract(part.value,'$.kind') IN "
            "('reasoning','reasoning_summary','reasoning_encrypted') " + activity_filter +
            " ORDER BY t.turn_id,membership.logical_item_ordinal,json_extract(part.value,'$.part_ordinal')",
            (view_id, *ids, *selected_ids),
        ).fetchall()
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
                "thinking_blocks": [],
                "assistant_text_sequences": [],
                "has_encrypted_reasoning": False,
                "tool_items": [],
                "activity_items": [],
                "created_at": created_at,
                "user_created_at": user_created_at,
                "updated_at": updated_at,
                "activity_stats": {
                    "duration_ms": None,
                    "item_count": 0,
                    "first_item_sequence": None,
                    "last_item_sequence": None,
                },
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
            execution_thread_id, execution_turn_id, job_id,
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
            projected_turn_ids.add(turn_id)
            if final_item_id is not None:
                projection["final_item_id"] = final_item_id
        if projected_turn_ids != set(result):
            raise RuntimeError(
                f"turns projection 缺少 canonical TurnRecord: {set(result) - projected_turn_ids}"
            )
        final_manifests: dict[str, object] = {}
        for (
            turn_id, final_sequence, text, truncated, message_id, message_turn_id,
            item_id, item_turn_id, semantic_kind, item_status,
            final_item_sequence, final_item_created_at,
            final_logical_item_ordinal, final_visible, final_source_kind,
            selected_turn_id, selected_final_sequence, has_canonical_projection,
            final_item_projection_manifest_json,
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
            if text is None or truncated is None:
                raise RuntimeError(f"final message projection 字段不完整: {turn_id}")
            text = strict_optional_text(
                text, field=f"message_projections.visible_text:{turn_id}"
            )
            truncated_value = strict_non_negative_int(
                truncated, field=f"message_projections.visible_text_truncated:{turn_id}"
            )
            if truncated_value not in {0, 1}:
                raise RuntimeError(f"message projection truncated 标记非法: {turn_id}")
            if turn_id not in result:
                raise TypeError(
                    f"final message projection 缺少 turns row: {turn_id}"
                )
            result[turn_id]["final_response_text"] = text or ""
            result[turn_id]["final_response_text_truncated"] = truncated_value == 1
            result[turn_id]["final_item_sequence"] = strict_non_negative_int(
                final_item_sequence,
                field=f"item_catalog.item_sequence:{turn_id}",
            )
            result[turn_id]["final_item_created_at"] = strict_text(
                final_item_created_at,
                field=f"item_catalog.created_at:{turn_id}",
            )
            result[turn_id]["final_logical_item_ordinal"] = strict_non_negative_int(
                final_logical_item_ordinal,
                field=f"context_view_items.logical_item_ordinal:{view_id}/{item_id}",
            )
            final_manifests[turn_id] = final_item_projection_manifest_json
        # 结构化文本使用写入时冻结的真实 Part 坐标，不把多个块压成伪造的 part:0。
        text_parts = {}
        for row in activity_rows:
            if row[3:5] != ("assistant_output", "structured_content"):
                continue
            text_parts[row[2]] = read_response_outline(connection, row[2])
            message = connection.execute(
                "SELECT message.message_sequence FROM item_catalog item JOIN messages message "
                "ON message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length "
                "WHERE item.item_id=?", (row[2],),
            ).fetchone()
            for part in text_parts[row[2]]:
                part["message_sequence"] = message[0] if message is not None else 0
        _merge_activity_items(result, tool_rows, activity_rows, text_parts)
        _merge_final_reasoning(
            result, final_reasoning_rows, final_manifests
        )
        # 坐标来自相同轻索引，正文补读和任意分页均不重新猜组首或从页首计时。
        for turn_id, projection in result.items():
            selected = [row for row in coordinates if row[10] == turn_id]
            members = []
            for member in projection["activity_items"]:
                identity = ItemProjectionIdentity(model_call_id=member.get("model_call_id"))
                matches = [row for row in selected if row[0] == member["item_id"] and row[2] == member["kind"]
                    and (row[3] is None or row[3] == member.get("content_block_index", member.get("block_ordinal")))
                    and (row[4] is None or row[4] == member.get("item_index"))
                    and (provider_tool_call_id(identity, row[11]) == provider_tool_call_id(identity, member.get("tool_call_id", ""))
                        if row[11] is not None else row[5] is None or row[5] == member.get("call_index"))]
                if len(matches) != 1:
                    if not matches:
                        continue
                    raise RuntimeError(f"活动部位坐标不唯一: {member['item_id']}/{member['kind']}")
                coordinate = matches[0]
                member.update(part_ordinal=coordinate[1], previous_created_at=coordinate[7] or projection["created_at"])
                if member["kind"] in {"reasoning", "reasoning_summary", "reasoning_encrypted", "tool_call", "tool_result"}:
                    member.update(activity_group_item_id=coordinate[8], activity_group_part_ordinal=coordinate[9])
                members.append(member)
            projection["activity_items"] = members
            if activity_limit is not None:
                projection["activity_has_more"] = has_more
                if has_more and selected:
                    projection["activity_continuation"] = min((row[6], row[1]) for row in selected)
        _finalize_projections(result)
        return result
