"""同一控制库事务中的有界 Thread 名称投影；不读取执行历史。"""

from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Literal


class ThreadCatalogRevisionChangedError(ValueError):
    """当前 Session 的 Thread 发布版本变化，不属于读取故障。"""

    def __init__(self, session_id: str, expected_revision: int, actual_revision: int) -> None:
        super().__init__(f"Thread 目录已更新，请重新查询: session_id={session_id}, "
                         f"expected_revision={expected_revision}, actual_revision={actual_revision}")

    def detail(self) -> dict[str, str]:
        return {"owner": "session_thread_catalog", "code": "stale_thread_catalog", "reason": "thread_revision", "message": str(self)}


@dataclass(frozen=True, slots=True)
class ThreadNameRow:
    thread_id: str
    kind: Literal["main", "child"]
    title: str | None
    created_at: datetime


def read_thread_name_page(
    connection: Connection,
    *,
    after_thread_id: str | None,
    limit: int,
) -> tuple[list[ThreadNameRow], bool]:
    """main 优先，其后按稳定 ID；只投影本页已发布 child 的冻结标题。"""
    result: list[ThreadNameRow] = []
    if after_thread_id is None:
        main = connection.execute(
            "SELECT thread_id, created_at FROM thread_catalog WHERE kind = 'main'",
        ).fetchone()
        result.append(
            ThreadNameRow(
                thread_id=main["thread_id"],
                kind="main",
                title=None,
                created_at=datetime.fromisoformat(main["created_at"]),
            )
        )
    rows = connection.execute(
        "SELECT thread.thread_id, thread.kind, thread.created_at, "
        "creation.state AS creation_state, "
        "COALESCE(member.title, CASE WHEN creation.purpose = 'fork' "
        "THEN json_extract(creation.request_intent_json, '$.display_title') END) AS title "
        "FROM thread_catalog AS thread "
        "LEFT JOIN thread_creation_records AS creation ON creation.child_thread_id = thread.thread_id "
        "LEFT JOIN collaboration_members AS member ON member.child_thread_id = thread.thread_id "
        "WHERE thread.kind = 'child' AND thread.thread_id > ? "
        "ORDER BY thread.thread_id LIMIT ?",
        (after_thread_id or "", limit + 1 - len(result)),
    ).fetchall()
    for row in rows:
        kind = row["kind"]
        if kind not in {"main", "child"}:
            raise RuntimeError(f"Thread 名称查询遇到非法 kind: {kind!r}")
        if kind == "child" and row["creation_state"] != "published":
            raise RuntimeError(
                f"已登记 child 缺少 published 创建记录: thread_id={row['thread_id']}"
            )
        result.append(
            ThreadNameRow(
                thread_id=row["thread_id"],
                kind=kind,
                title=row["title"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )
    return result[:limit], len(result) > limit
