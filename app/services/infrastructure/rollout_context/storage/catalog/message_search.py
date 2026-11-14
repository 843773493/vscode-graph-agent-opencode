"""在同一历史 view 中有界取得消息正文 locator，不保存搜索正文。"""

import sqlite3
from time import monotonic
from typing import Literal

from app.core.sqlite_state import load_sql
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.storage.catalog.turn_projections import (
    VISIBLE_NORMAL_TURN_PREDICATE,
)

_CARRIERS_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/history_message_carriers.sql",
) + load_sql("app.services.infrastructure.rollout_context.storage", "migrations/content_carrier_replacements.sql")
_SURVIVOR_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "migrations/internal_history/survivor.sql",
)
# 无模型 scope 的 item 各自独立；其余用 raw/scoped ID 缩小候选后核对权威还原身份。
# 较早载体必须仍在当前 view 存活，查重范围不能随本页的 lower_ordinal 截断。
_TOOL_REPRESENTATIVE_SQL = """
(membership.part_scope IS NULL OR NOT EXISTS (
    SELECT 1 FROM item_catalog AS previous INDEXED BY item_catalog_model_tool_index
    CROSS JOIN carriers AS prior ON prior.item_id = previous.item_id
    WHERE (json_extract(previous.projection_manifest_json, '$.projection_identity.model_call_id') = membership.part_scope
        OR json_extract(previous.projection_manifest_json, '$.projection_identity.model_call_id') IS NULL)
        AND previous.semantic_kind = 'tool_result' AND previous.turn_id = item.turn_id
        AND previous.tool_call_id IN (
            search_tool_call_id(membership.part_scope, item.tool_call_id),
            membership.part_scope || ':tool-call:' || search_tool_call_id(membership.part_scope, item.tool_call_id)
        )
        AND previous.item_sequence < item.item_sequence AND prior.part_scope = membership.part_scope
        AND search_tool_call_id(prior.part_scope, previous.tool_call_id) =
            search_tool_call_id(membership.part_scope, item.tool_call_id)
))
"""


def message_search_candidates(
    connection: sqlite3.Connection, view_id: str, *, lower_ordinal: int,
    limit: int, deadline: float, item_id: str | None = None,
    category: Literal["message", "tool_result"] = "message",
) -> list[tuple[int, str, str, int]]:
    expired = False

    def check_deadline() -> int:
        nonlocal expired
        expired = monotonic() >= deadline
        return int(expired)

    connection.set_progress_handler(check_deadline, 1000)
    # 与原 activity 归并一致：同一逻辑结果取 item_sequence 最早的载体。
    # 实时回调与 checkpoint 的落盘先后并不固定，不能按载体类型猜测原详情身份。
    connection.create_function("search_tool_call_id", 2, lambda model, call:
        provider_tool_call_id(ItemProjectionIdentity(model_call_id=model), call))
    tools = category == "tool_result"
    try:
        return connection.execute(
            _CARRIERS_SQL +
            "SELECT membership.logical_item_ordinal,item.item_id,item.turn_id,item.jsonl_length "
            "FROM carriers AS membership "
            "CROSS JOIN item_catalog AS item ON item.item_id=membership.item_id "
            "JOIN context_view_turns AS cvt ON cvt.view_id=membership.view_id AND cvt.turn_id=item.turn_id "
            "WHERE membership.logical_item_ordinal>=:lower_ordinal "
            + (f"AND item.semantic_kind='tool_result' AND {_TOOL_REPRESENTATIVE_SQL} " if tools else
               "AND item.semantic_kind IN ('user_input','assistant_output') "
               "AND item.wire_role IN ('user','assistant') "
               "AND item.payload_kind IN ('text','structured_content') ") +
            f"AND {VISIBLE_NORMAL_TURN_PREDICATE} AND {_SURVIVOR_SQL} "
            + ("AND item.item_id=:item_id " if item_id is not None else "")
            # 成员块按逻辑序号有序发布；沿块内索引取页，避免先检查全部候选再全量排序。
            + "ORDER BY membership.block_ordinal,membership.logical_item_ordinal LIMIT :limit",
            {"view_id": view_id, "lower_ordinal": lower_ordinal, "item_id": item_id, "limit": limit},
        ).fetchall()
    except sqlite3.OperationalError as error:
        if expired:
            raise TimeoutError("消息查询达到 SQLite 读取期限") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.create_function("search_tool_call_id", 2, None)


def tool_result_metadata(
    connection: sqlite3.Connection, view_id: str, *, after: int, limit: int,
    deadline: float, item_id: str | None = None,
) -> list[tuple[int, str, str, str, str | None, str, str]]:
    """共用正文搜索的可见载体选择；第二条查询只读原调用索引和版本元数据。"""
    candidates = message_search_candidates(connection, view_id, lower_ordinal=after + 1,
        limit=limit, deadline=deadline, item_id=item_id, category="tool_result")
    if not candidates:
        return []
    placeholders = ",".join("?" for _ in candidates)
    connection.create_function("search_tool_call_id", 2, lambda model, call:
        provider_tool_call_id(ItemProjectionIdentity(model_call_id=model), call))
    connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    try:
        rows = connection.execute(
            "SELECT item.item_id,item.turn_id,item.tool_call_id,"
            "json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id'),"
            # 先按模型调用定位声明；从 Turn 消息起扫会对本页每条结果重扫全部调用历史。
            "item.status,COALESCE((SELECT call.tool_name "
            "FROM item_catalog source INDEXED BY item_catalog_model_tool_index "
            "CROSS JOIN messages assistant ON assistant.jsonl_offset=source.jsonl_offset "
            "AND assistant.jsonl_length=source.jsonl_length "
            "CROSS JOIN tool_calls call ON call.assistant_message_sequence=assistant.message_sequence "
            "WHERE assistant.turn_id=item.turn_id AND "
            "json_extract(source.projection_manifest_json,'$.projection_identity.model_call_id') IS "
            "json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id') "
            "AND search_tool_call_id(json_extract(source.projection_manifest_json,'$.projection_identity.model_call_id'),call.tool_call_id)="
            "search_tool_call_id(json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id'),item.tool_call_id) "
            "ORDER BY call.assistant_message_sequence LIMIT 1),item.tool_call_id) "
            f"FROM item_catalog item WHERE item.item_id IN ({placeholders})",
            [row[1] for row in candidates],
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        return [(ordinal, *by_id[candidate_id]) for ordinal, candidate_id, _turn, _length in candidates]
    except sqlite3.OperationalError as error:
        if monotonic() >= deadline:
            raise TimeoutError("工具结果目录达到 SQLite 读取期限") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.create_function("search_tool_call_id", 2, None)
