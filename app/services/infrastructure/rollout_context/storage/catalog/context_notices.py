"""从已派发请求索引读取内部提示标记；不读取提示正文或 assembly 快照。"""

import sqlite3
from time import monotonic
from typing import NamedTuple


class ContextNoticeMetadata(NamedTuple):
    item_id: str | None
    item_sequence: int | None
    logical_item_ordinal: int | None
    created_at: str
    source_id: str
    kind: str
    model_call_id: str
    turn_id: str | None
    execution_id: str
    before_item_id: str | None
    assembly_id: str
    contribution_id: str | None
    session_id: str
    thread_id: str
    source_revision: str | None
    content_length: int
    availability: str
    anchor: str
    anchor_ordinal: int | None
    plan_ordinal: int
    call_order: int
    call_upper_bound: int
    truncated: bool = False


def context_notice_metadata(
    connection: sqlite3.Connection, view_id: str, *, turn_ids: list[str],
    execution_ids: list[str], limit: int = 128,
    after: tuple[int, int] = (0, -1), call_upper_bound: int | None = None,
    model_call_id: str | None = None, anchor: str | None = None,
) -> list[ContextNoticeMetadata]:
    if not turn_ids and not execution_ids:
        return []
    # 先按本页执行查请求，再读这些请求的 selection。前驱只查可见 lineage
    # 中相邻的派发请求，不能把“历史上用过 A”当作 A→B→A 最后一次的去重条件。
    parameters: dict[str, str | int | None] = {
        "view_id": view_id, "limit": limit + 1,
        "after_call": after[0], "after_plan": after[1], "call_upper_bound": call_upper_bound,
        "model_call_id": model_call_id, "anchor": anchor,
    }
    parameters.update({f"turn_{index}": value for index, value in enumerate(turn_ids)})
    parameters.update({f"execution_{index}": value for index, value in enumerate(execution_ids)})
    turn_slots = ",".join(f":turn_{index}" for index in range(len(turn_ids)))
    execution_slots = ",".join(f":execution_{index}" for index in range(len(execution_ids)))
    scope = []
    if turn_ids:
        scope.append(f"execution.turn_id IN ({turn_slots})")
    if execution_ids:
        scope.append(f"execution.execution_id IN ({execution_slots})")
    deadline = monotonic() + 1.0
    connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    try:
        rows = connection.execute(_notice_query("(" + " OR ".join(scope) + ") AND (:model_call_id IS NULL OR call.model_call_id=:model_call_id)"),
            parameters,
        ).fetchall()
        result = [ContextNoticeMetadata(*row) for row in rows[:limit]]
        if len(rows) > limit:
            result[-1] = result[-1]._replace(truncated=True)
        return result
    except sqlite3.OperationalError as error:
        if monotonic() >= deadline:
            raise TimeoutError("上下文标记查询达到 SQLite 读取期限") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _notice_query(scope: str) -> str:
    # contribution 的 binding manifest 是封存元数据，不读取 snapshot_json/DAG。
    # overlay 通过 selection.contribution_id 唯一绑定 backing，不另行枚举来源。
    # 对 context_view_items 使用相关定点子查询；LEFT JOIN 会让 SQLite 物化整个历史 view。
    return """
    WITH bounds AS (
        SELECT COALESCE(:call_upper_bound, (SELECT MAX(rowid) FROM model_calls
            WHERE dispatch_state IN ('dispatched','completed','failed'))) AS upper_bound
    ), page_calls AS MATERIALIZED (
        SELECT call.rowid AS call_order, call.model_call_id, call.assembly_id,
            bounds.upper_bound AS call_upper_bound,
            call.session_id, call.thread_id, execution.execution_id, execution.turn_id,
            call.created_at,
            (SELECT previous.assembly_id FROM model_calls previous
             JOIN executions owner ON owner.execution_id=previous.execution_id
             WHERE previous.rowid<call.rowid
               AND previous.dispatch_state IN ('dispatched','completed','failed')
               AND (EXISTS (SELECT 1 FROM context_view_turns visible
                            WHERE visible.view_id=:view_id AND visible.turn_id=owner.turn_id)
                    OR (owner.turn_id IS NULL AND EXISTS (
                        SELECT 1 FROM item_catalog carrier CROSS JOIN context_view_items member
                        WHERE carrier.execution_id=owner.execution_id AND member.item_id=carrier.item_id
                          AND member.view_id=:view_id AND member.visible=1)))
             ORDER BY previous.rowid DESC LIMIT 1) AS previous_assembly_id
        FROM bounds CROSS JOIN executions execution CROSS JOIN model_calls call
          ON call.execution_id=execution.execution_id
        WHERE (""" + scope + """) AND call.rowid BETWEEN :after_call AND bounds.upper_bound
          AND call.dispatch_state IN ('dispatched','completed','failed')
          AND (EXISTS (SELECT 1 FROM context_view_turns visible
                       JOIN context_view_items root ON root.view_id=visible.view_id
                         AND root.item_id=visible.root_input_item_id AND root.visible=1
                       WHERE visible.view_id=:view_id AND visible.turn_id=execution.turn_id)
               OR (execution.turn_id IS NULL AND EXISTS (
                   SELECT 1 FROM item_catalog carrier CROSS JOIN context_view_items member
                   WHERE carrier.execution_id=execution.execution_id AND member.item_id=carrier.item_id
                     AND member.view_id=:view_id AND member.visible=1)))
          AND NOT EXISTS (SELECT 1 FROM model_calls retry
                          WHERE retry.assembly_id=call.assembly_id AND retry.rowid<call.rowid
                            AND retry.dispatch_state IN ('dispatched','completed','failed'))
    ), candidates AS MATERIALIZED (
        SELECT call.*, selected.plan_ordinal, selected.ref_type, selected.ref_id,
            selected.ref_thread_id, selected.contribution_id, selected.source_revision,
            COALESCE(selected.content_length, item.payload_length) AS content_length,
            selected.content_hash, selected.availability, selected.base_delta_role,
            item.item_id, item.item_sequence, item.created_at AS item_created_at,
            CASE WHEN selected.ref_type='canonical_item'
                 THEN json_extract(item.producer_ref_json,'$.producer_id')
                 ELSE COALESCE(json_extract(source.binding_manifest_json,'$.source_binding.source_ref'),
                               source.contribution_id) END AS source_id,
            CASE WHEN selected.ref_type='canonical_item'
                 THEN COALESCE(json_extract(item.projection_manifest_json,
                   '$.extensions."boxteam.checkpoint.annotations".value.structured_prompt_kind'),'runtime_notice')
                 ELSE source.source_kind END AS kind
        FROM page_calls call CROSS JOIN context_assembly_selections selected
          ON selected.assembly_id=call.assembly_id
        LEFT JOIN item_catalog item ON selected.ref_type='canonical_item' AND item.item_id=selected.ref_id
        LEFT JOIN context_assembly_contributions source
          ON source.assembly_id=selected.assembly_id AND source.contribution_id=selected.contribution_id
        WHERE selected.included=1 AND selected.protection='public'
          AND ((selected.ref_type='canonical_item' AND item.semantic_kind='runtime_notice')
               OR (selected.ref_type='request_only'
                   AND source.contribution_kind IN ('prompt','notice','overlay_base','overlay_delta')))
    ), effective AS MATERIALIZED (
        SELECT candidate.* FROM candidates candidate
        WHERE candidate.ref_type='canonical_item' OR NOT EXISTS (
            SELECT 1 FROM candidates later
            WHERE later.assembly_id=candidate.assembly_id AND later.ref_type='request_only'
              AND later.source_id=candidate.source_id
              AND later.base_delta_role=candidate.base_delta_role
              AND later.plan_ordinal>candidate.plan_ordinal
        )
    ), changes AS (
        SELECT candidate.* FROM effective candidate
        WHERE kind NOT IN ('delegated_task','generated_session_result')
          AND NOT EXISTS (
            SELECT 1 FROM context_assembly_selections prior
            LEFT JOIN context_assembly_contributions source
              ON source.assembly_id=prior.assembly_id AND source.contribution_id=prior.contribution_id
            WHERE prior.assembly_id=candidate.previous_assembly_id AND prior.included=1
              AND prior.ref_type=candidate.ref_type AND prior.ref_thread_id=candidate.ref_thread_id
              AND (candidate.ref_type='canonical_item' AND prior.ref_id=candidate.ref_id
                   OR candidate.ref_type='request_only' AND prior.base_delta_role=candidate.base_delta_role
                     AND COALESCE(json_extract(source.binding_manifest_json,'$.source_binding.source_ref'),
                                  source.contribution_id)=candidate.source_id
                     AND NOT EXISTS (
                       SELECT 1 FROM context_assembly_selections later
                       JOIN context_assembly_contributions next_source
                         ON next_source.assembly_id=later.assembly_id AND next_source.contribution_id=later.contribution_id
                       WHERE later.assembly_id=prior.assembly_id AND later.included=1
                         AND later.ref_type='request_only' AND later.base_delta_role=prior.base_delta_role
                         AND later.plan_ordinal>prior.plan_ordinal
                         AND COALESCE(json_extract(next_source.binding_manifest_json,'$.source_binding.source_ref'),
                                      next_source.contribution_id)=candidate.source_id))
              AND prior.source_revision IS candidate.source_revision
              AND prior.content_hash IS candidate.content_hash
          )
    )
    , anchors AS MATERIALIZED (
        SELECT call.model_call_id, root.root_input_item_id,
            (SELECT member.logical_item_ordinal FROM context_view_items member
             WHERE member.view_id=:view_id AND member.item_id=root.root_input_item_id) AS root_ordinal,
            (SELECT member.visible FROM context_view_items member
             WHERE member.view_id=:view_id AND member.item_id=root.root_input_item_id) AS root_visible,
            NOT EXISTS (
                SELECT 1 FROM executions owner CROSS JOIN model_calls earlier
                  ON earlier.execution_id=owner.execution_id
                WHERE owner.turn_id=call.turn_id AND earlier.rowid<call.call_order
                  AND earlier.dispatch_state IN ('dispatched','completed','failed')
            ) AS first_in_turn,
            (SELECT output.item_id FROM item_catalog output INDEXED BY item_catalog_model_tool_index
             JOIN context_view_items position ON position.item_id=output.item_id
               AND position.view_id=:view_id AND position.visible=1
             WHERE json_extract(output.projection_manifest_json,'$.projection_identity.model_call_id')=+call.model_call_id
             ORDER BY position.logical_item_ordinal LIMIT 1) AS output_id,
            (SELECT MAX(position.logical_item_ordinal)+1
             FROM context_assembly_selections input CROSS JOIN context_view_items position
             WHERE input.assembly_id=call.assembly_id AND input.included=1
               AND input.ref_type='canonical_item' AND position.item_id=input.ref_id
               AND position.view_id=:view_id AND position.visible=1) AS input_end
        FROM page_calls call
        LEFT JOIN context_view_turns root ON root.view_id=:view_id AND root.turn_id=call.turn_id
    )
    SELECT candidate.item_id,candidate.item_sequence,
        (SELECT member.logical_item_ordinal FROM context_view_items member
         WHERE member.view_id=:view_id AND member.item_id=candidate.item_id),
        COALESCE(candidate.item_created_at,candidate.created_at),candidate.source_id,candidate.kind,
        candidate.model_call_id,candidate.turn_id,candidate.execution_id,
        CASE WHEN anchor.root_visible=1 AND (candidate.kind='checkpoint_reminder' OR anchor.first_in_turn)
             THEN anchor.root_input_item_id ELSE anchor.output_id END,
        candidate.assembly_id,candidate.contribution_id,candidate.session_id,candidate.thread_id,
        candidate.source_revision,candidate.content_length,candidate.availability,
        CASE WHEN anchor.root_visible=1 AND (candidate.kind='checkpoint_reminder' OR anchor.first_in_turn)
             THEN 'before_user_message' ELSE 'before_model_call' END AS notice_anchor,
        CASE WHEN anchor.root_visible=1 AND (candidate.kind='checkpoint_reminder' OR anchor.first_in_turn)
             THEN anchor.root_ordinal ELSE COALESCE(
               (SELECT output.logical_item_ordinal FROM context_view_items output
                WHERE output.view_id=:view_id AND output.item_id=anchor.output_id),anchor.input_end,
               (SELECT MIN(position.logical_item_ordinal) FROM item_catalog carrier
                CROSS JOIN context_view_items position WHERE carrier.execution_id=candidate.execution_id
                  AND position.item_id=carrier.item_id AND position.view_id=:view_id AND position.visible=1)) END,
        candidate.plan_ordinal,candidate.call_order,candidate.call_upper_bound
    FROM changes candidate JOIN anchors anchor ON anchor.model_call_id=candidate.model_call_id
    WHERE (candidate.item_id IS NULL OR EXISTS (
        SELECT 1 FROM context_view_items member
        WHERE member.view_id=:view_id AND member.item_id=candidate.item_id AND member.visible=1))
      AND (candidate.call_order,candidate.plan_ordinal)>(:after_call,:after_plan)
      AND (:anchor IS NULL OR notice_anchor=:anchor)
    ORDER BY candidate.call_order,candidate.plan_ordinal LIMIT :limit
    """
