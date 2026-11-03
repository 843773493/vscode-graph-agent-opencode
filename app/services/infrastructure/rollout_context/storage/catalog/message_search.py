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
    "app.services.infrastructure.rollout_context.storage", "sql/history_message_carriers.sql",
) + load_sql("app.services.infrastructure.rollout_context.storage", "sql/content_carrier_replacements.sql")
_SURVIVOR_SQL = load_sql(
    "app.services.infrastructure.rollout_context.storage", "sql/internal_history/survivor.sql",
)


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
    candidates = (
        ", ranked_tools AS MATERIALIZED (SELECT membership.*, ROW_NUMBER() OVER ("
        "PARTITION BY item.turn_id, COALESCE(membership.part_scope, item.item_id), "
        "search_tool_call_id(membership.part_scope,item.tool_call_id) "
        "ORDER BY item.item_sequence) AS result_rank FROM carriers AS membership "
        "JOIN item_catalog AS item ON item.item_id=membership.item_id "
        "WHERE item.semantic_kind='tool_result') "
        if tools else ""
    )
    try:
        return connection.execute(
            _CARRIERS_SQL + candidates +
            "SELECT membership.logical_item_ordinal,item.item_id,item.turn_id,item.jsonl_length "
            f"FROM {'ranked_tools' if tools else 'carriers'} AS membership "
            "JOIN item_catalog AS item ON item.item_id=membership.item_id "
            "JOIN context_view_turns AS cvt ON cvt.view_id=membership.view_id AND cvt.turn_id=item.turn_id "
            "WHERE membership.logical_item_ordinal>=:lower_ordinal "
            + ("AND membership.result_rank=1 " if tools else
               "AND item.semantic_kind IN ('user_input','assistant_output') "
               "AND item.wire_role IN ('user','assistant') "
               "AND item.payload_kind IN ('text','structured_content') ") +
            f"AND {VISIBLE_NORMAL_TURN_PREDICATE} AND {_SURVIVOR_SQL} "
            + ("AND item.item_id=:item_id " if item_id is not None else "")
            + "ORDER BY membership.logical_item_ordinal LIMIT :limit",
            {"view_id": view_id, "lower_ordinal": lower_ordinal, "item_id": item_id, "limit": limit},
        ).fetchall()
    except sqlite3.OperationalError as error:
        if expired:
            raise TimeoutError("消息查询达到 SQLite 读取期限") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.create_function("search_tool_call_id", 2, None)
