"""在同一历史 view 中有界取得消息正文 locator，不保存搜索正文。"""

import sqlite3
from time import monotonic

from app.core.sqlite_state import load_sql
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
) -> list[tuple[int, str, str, int]]:
    expired = False

    def check_deadline() -> int:
        nonlocal expired
        expired = monotonic() >= deadline
        return int(expired)

    connection.set_progress_handler(check_deadline, 1000)
    try:
        return connection.execute(
            _CARRIERS_SQL +
            "SELECT membership.logical_item_ordinal,item.item_id,item.turn_id,item.jsonl_length "
            "FROM carriers AS membership "
            "JOIN item_catalog AS item ON item.item_id=membership.item_id "
            "JOIN context_view_turns AS cvt ON cvt.view_id=membership.view_id AND cvt.turn_id=item.turn_id "
            "WHERE membership.logical_item_ordinal>=:lower_ordinal "
            "AND item.semantic_kind IN ('user_input','assistant_output') "
            "AND item.wire_role IN ('user','assistant') "
            "AND item.payload_kind IN ('text','structured_content') "
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
