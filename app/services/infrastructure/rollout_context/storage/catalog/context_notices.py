"""从已派发请求索引读取内部提示标记；不读取提示正文或 assembly 快照。"""

import sqlite3
from time import monotonic
from typing import NamedTuple


class ContextNoticeMetadata(NamedTuple):
    item_id: str
    item_sequence: int
    logical_item_ordinal: int
    created_at: str
    source_id: str
    kind: str
    model_call_id: str
    turn_id: str | None
    execution_id: str
    before_item_id: str | None
    truncated: bool = False


def context_notice_metadata(
    connection: sqlite3.Connection, view_id: str, *, turn_ids: list[str],
    execution_ids: list[str], limit: int = 128,
) -> list[ContextNoticeMetadata]:
    if not turn_ids and not execution_ids:
        return []
    # 先限定本页执行及 runtime_notice，再按 ID 查 selection；不遍历请求中的
    # 普通历史引用。pending 来源和后续重复带入的相同 Item 不产生新标记。
    turn_slots = ",".join("?" for _ in turn_ids)
    execution_slots = ",".join("?" for _ in execution_ids)
    scope = []
    if turn_ids:
        scope.append(f"execution.turn_id IN ({turn_slots})")
    if execution_ids:
        scope.append(f"execution.execution_id IN ({execution_slots})")
    deadline = monotonic() + 1.0
    connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    try:
        rows = connection.execute(
            "SELECT item.item_id,item.item_sequence,member.logical_item_ordinal,item.created_at,"
            "json_extract(item.producer_ref_json,'$.producer_id'),"
            "COALESCE(json_extract(item.projection_manifest_json,"
            "'$.extensions.\"boxteam.checkpoint.annotations\".value.structured_prompt_kind'),'runtime_notice'),"
            "call.model_call_id,execution.turn_id,execution.execution_id,"
            "(SELECT output.item_id FROM item_catalog output INDEXED BY item_catalog_model_tool_index "
            "JOIN context_view_items position ON position.item_id=output.item_id AND position.view_id=? AND position.visible=1 "
            # 去掉 TEXT 列的比较亲和性，才能命中无类型的 JSON 表达式索引。
            "WHERE json_extract(output.projection_manifest_json,'$.projection_identity.model_call_id')=+call.model_call_id "
            "ORDER BY position.logical_item_ordinal LIMIT 1) "
            "FROM executions execution CROSS JOIN model_calls call ON call.execution_id=execution.execution_id "
            "CROSS JOIN item_catalog item INDEXED BY item_catalog_semantic_index "
            "CROSS JOIN context_assembly_selections selected ON selected.assembly_id=call.assembly_id AND selected.ref_id=item.item_id "
            "CROSS JOIN context_view_items member ON member.item_id=item.item_id AND member.view_id=? "
            "WHERE (" + " OR ".join(scope) + ") "
            "AND call.dispatch_state IN ('dispatched','completed') AND selected.included=1 "
            "AND selected.ref_type='canonical_item' AND item.semantic_kind='runtime_notice' AND member.visible=1 "
            "AND COALESCE(json_extract(item.projection_manifest_json,'$.source_manifest.protection'),'public')='public' "
            "AND COALESCE(json_extract(item.projection_manifest_json,"
            "'$.extensions.\"boxteam.checkpoint.annotations\".value.structured_prompt_kind'),'') "
            "NOT IN ('delegated_task','generated_session_result') "
            "AND NOT EXISTS (SELECT 1 FROM context_assembly_selections prior "
            "JOIN model_calls previous ON previous.assembly_id=prior.assembly_id "
            "WHERE prior.ref_type=selected.ref_type AND prior.ref_id=selected.ref_id "
            "AND prior.ref_thread_id=selected.ref_thread_id AND prior.included=1 "
            "AND previous.dispatch_state IN ('dispatched','completed') "
            "AND previous.rowid<call.rowid) "
            "ORDER BY call.rowid,selected.plan_ordinal LIMIT ?",
            (view_id, view_id, *turn_ids, *execution_ids, limit + 1),
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
