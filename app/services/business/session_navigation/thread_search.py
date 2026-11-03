"""原 catalog 和 SessionThread owner 的渐进名称查询；不加载 cold runtime。"""

import asyncio
import base64
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.fuzzy import is_subsequence
from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.session_navigation.thread_search import (
    SessionThreadSearchResultDTO,
    SessionThreadSearchResultsDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_service import SessionService

_MAX_SCANNED_NODES = 64
_MAX_SCANNED_THREADS = 512
_QUERY_SECONDS = 1.0


class ThreadSearchCursorError(ValueError):
    """原 Thread 名称游标校验点的具名拒绝，API 保留既有409。"""

    def __init__(self, reason: Literal["malformed", "owner_mismatch", "query_mismatch", "catalog_revision", "position_out_of_bounds"], message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = "stale_thread_cursor" if reason == "catalog_revision" else "invalid_thread_cursor"

    def detail(self) -> dict[str, str]:
        return {"owner": "session_thread_search", "code": self.code, "reason": self.reason, "message": str(self)}


class _ThreadSearchCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    workspace_id: str
    gateway_id: str
    query: str
    catalog_revision: int
    node_offset: int = Field(default=0, ge=0)
    after_thread_id: str | None = None
    thread_revision: int | None = Field(default=None, ge=1)


async def search_session_threads(
    catalog: SessionCatalogReadService,
    sessions: SessionService,
    *,
    gateway_id: str,
    query: str,
    limit: int,
    cursor: str | None,
    deadline_at: float | None = None,
) -> SessionThreadSearchResultsDTO:
    """分页保留查询/目录版本和当前 Thread 版本，不假称跨库全局快照。"""
    query = query.strip().casefold()
    if not query:
        raise ValueError("Thread 名称查询不能为空")
    if not 1 <= limit <= 100:
        raise ValueError("Thread 名称查询 limit 必须在 1 到 100 之间")
    deadline = monotonic() + _QUERY_SECONDS
    budget = NameSearchBudget.create(deadline_at)
    snapshot = await catalog.export_index(check=budget.check)
    state = _ThreadSearchCursor(
        workspace_id=sessions.workspace_id,
        gateway_id=gateway_id,
        query=query,
        catalog_revision=snapshot.catalog_revision,
    )
    if cursor is not None:
        try:
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            previous = _ThreadSearchCursor.model_validate_json(raw)
        except ValueError as error:
            raise ThreadSearchCursorError("malformed", "Thread 搜索 cursor 格式无效") from error
        if previous.workspace_id != state.workspace_id or previous.gateway_id != state.gateway_id:
            raise ThreadSearchCursorError("owner_mismatch", "Thread 搜索 cursor 与查询或目录版本不匹配，请重新查询")
        if previous.query != state.query:
            raise ThreadSearchCursorError("query_mismatch", "Thread 搜索 cursor 与查询或目录版本不匹配，请重新查询")
        if previous.catalog_revision != state.catalog_revision:
            raise ThreadSearchCursorError("catalog_revision", "Thread 搜索 cursor 与查询或目录版本不匹配，请重新查询")
        state = previous
    if state.node_offset > len(snapshot.items):
        raise ThreadSearchCursorError("position_out_of_bounds", "Thread 搜索 cursor 目录位置越界")
    items: list[SessionThreadSearchResultDTO] = []
    scanned_nodes = scanned_threads = 0
    scanned_session_ids: set[str] = set()
    reason: Literal["result_limit", "scan_limit", "deadline"] | None = None
    while state.node_offset < len(snapshot.items):
        budget.check()
        if len(items) >= limit:
            reason = "result_limit"
            break
        if (
            scanned_nodes >= _MAX_SCANNED_NODES
            or scanned_threads >= _MAX_SCANNED_THREADS
        ):
            reason = "scan_limit"
            break
        if monotonic() >= deadline:
            reason = "deadline"
            break
        node = snapshot.items[state.node_offset]
        scanned_nodes += 1
        if node.session is not None:
            # 期限覆盖准入等待；原受保护 worker 仍结算后才释放 Session 句柄。
            waiting = asyncio.timeout(budget.remaining())
            try:
                async with waiting:
                    revision, rows, has_more = await sessions.thread_name_page(
                        node.node_id,
                        after_thread_id=state.after_thread_id,
                        limit=min(128, _MAX_SCANNED_THREADS - scanned_threads),
                        expected_revision=state.thread_revision,
                    )
            except TimeoutError:
                if waiting.expired():
                    budget.check()
                raise
            scanned_session_ids.add(node.node_id)
            state.thread_revision = revision
            consumed = 0
            for row in rows:
                budget.check()
                state.after_thread_id = row.thread_id if row.kind == "child" else ""
                consumed += 1
                scanned_threads += 1
                title = node.name if row.kind == "main" else row.title or row.thread_id
                if any(
                    is_subsequence(query, value.casefold())
                    for value in (title, row.thread_id, node.node_id)
                ):
                    items.append(
                        SessionThreadSearchResultDTO(
                            workspace_id=sessions.workspace_id,
                            session_id=node.node_id,
                            thread_id=row.thread_id,
                            kind=row.kind,
                            title=title,
                            session_title=node.name,
                            created_at=row.created_at,
                            thread_uri=thread_resource_uri(
                                sessions.workspace_id,
                                node.node_id,
                                row.thread_id,
                                gateway_authority=gateway_id,
                            ),
                        )
                    )
                if len(items) >= limit:
                    break
            if has_more or consumed < len(rows):
                continue
        state.node_offset += 1
        state.after_thread_id = None
        state.thread_revision = None
    # 目录变化不能让旧的名称/归属混入本页；原 owner 负责发布版本。
    if sessions.path_resolver.revision != snapshot.catalog_revision:
        raise ThreadSearchCursorError("catalog_revision", "Thread 搜索期间目录已更新，请重新查询")
    complete = state.node_offset == len(snapshot.items)
    budget.check()
    return SessionThreadSearchResultsDTO(
        catalog_revision=snapshot.catalog_revision,
        items=items,
        cursor=None
        if complete
        else base64.urlsafe_b64encode(state.model_dump_json().encode())
        .decode()
        .rstrip("="),
        complete=complete,
        limited_reason=reason,
        scanned_sessions=len(scanned_session_ids),
        scanned_threads=scanned_threads,
    )
