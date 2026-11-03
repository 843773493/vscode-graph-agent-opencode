"""在 catalog / SessionThread 归属内编排唯一 canonical 正文查询。"""

import asyncio
import base64
import binascii
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.session_navigation.thread_search import (
    SessionThreadSearchResultDTO,
)
from app.schemas.internal_v2.turn_search import (
    TurnMessageSearchRequest,
    WorkspaceMessageMatchDTO,
    WorkspaceMessageSearchResultsDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_service import SessionService
from app.services.business.session_turn_history.service import SessionTurnHistoryService
from app.services.infrastructure.rollout_history.message_search import (
    MessageSearchBudget,
)
from app.services.infrastructure.turn_history.models import InvalidTurnCursorError

MAX_SCANNED_NODES = 64
MAX_SCANNED_THREADS = 16


class _WorkspaceMessageCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    gateway_id: str
    workspace_id: str
    query: str
    category: Literal["message", "tool_result"] = "message"
    catalog_revision: int
    node_offset: int = Field(default=0, ge=0)
    after_thread_id: str | None = None
    thread_revision: int | None = Field(default=None, ge=1)
    history_cursor: str | None = None


async def search_workspace_messages(
    catalog: SessionCatalogReadService, sessions: SessionService, history: SessionTurnHistoryService,
    request: TurnMessageSearchRequest, *, gateway_id: str, deadline_at: float | None = None,
) -> WorkspaceMessageSearchResultsDTO:
    """各 Thread 各自读取时的快照；已完成分支新增的正文需重新查询。"""
    admission = NameSearchBudget.create(deadline_at)
    budget = MessageSearchBudget.create()
    snapshot = await catalog.export_index(check=admission.check)
    state = _WorkspaceMessageCursor(
        gateway_id=gateway_id, workspace_id=sessions.workspace_id,
        query=request.query, category=request.category, catalog_revision=snapshot.catalog_revision,
    )
    if request.cursor is not None:
        try:
            raw = base64.b64decode(request.cursor + "=" * (-len(request.cursor) % 4), altchars=b"-_", validate=True)
            previous = _WorkspaceMessageCursor.model_validate_json(raw)
        except (ValueError, binascii.Error, ValidationError) as error:
            raise InvalidTurnCursorError("工作区消息搜索 cursor 格式无效") from error
        if any(getattr(previous, field) != getattr(state, field)
               for field in ("gateway_id", "workspace_id", "query", "category")):
            raise InvalidTurnCursorError("工作区消息搜索 cursor 不属于本次 owner 或查询")
        if previous.catalog_revision != state.catalog_revision:
            raise ValueError("工作区消息搜索目录已更新，请重新查询")
        state = previous
    if state.node_offset > len(snapshot.items):
        raise InvalidTurnCursorError("工作区消息搜索 cursor 目录位置越界")
    items: list[WorkspaceMessageMatchDTO] = []
    scanned_sessions: set[str] = set()
    scanned_nodes = scanned_threads = 0
    reason = None
    while state.node_offset < len(snapshot.items):
        admission.check()
        if len(items) >= request.limit:
            reason = "result_limit"
            break
        if scanned_nodes >= MAX_SCANNED_NODES:
            reason = "scan_limit"
            break
        if scanned_threads >= MAX_SCANNED_THREADS:
            reason = "thread_limit"
            break
        if monotonic() >= budget.deadline:
            reason = "deadline"
            break
        node = snapshot.items[state.node_offset]
        scanned_nodes += 1
        if node.session is not None:
            # 同一控制库 revision 保护当前 Session 的 keyset，正文续页仍由原历史 owner 核验。
            async with asyncio.timeout(admission.remaining()):
                revision, rows, has_more = await sessions.thread_name_page(
                    node.node_id, after_thread_id=state.after_thread_id,
                    limit=1, expected_revision=state.thread_revision,
                )
                state.thread_revision = revision
                if rows:
                    row, = rows
                    page = await history.search_messages(node.node_id, row.thread_id,
                        TurnMessageSearchRequest(query=request.query, limit=request.limit - len(items),
                                                 cursor=state.history_cursor, category=request.category), budget=budget)
                    scanned_sessions.add(node.node_id)
                    scanned_threads += 1
                    target = SessionThreadSearchResultDTO(
                        workspace_id=sessions.workspace_id, session_id=node.node_id,
                        thread_id=row.thread_id, kind=row.kind, created_at=row.created_at,
                        title=node.name if row.kind == "main" else row.title or row.thread_id,
                        session_title=node.name,
                        thread_uri=thread_resource_uri(sessions.workspace_id, node.node_id, row.thread_id,
                                                       gateway_authority=gateway_id),
                    )
                    items.extend(WorkspaceMessageMatchDTO(thread=target, match=hit) for hit in page.items)
                    state.history_cursor = page.cursor
                    if not page.complete:
                        reason = page.limited_reason
                        break
                    state.after_thread_id = row.thread_id if row.kind == "child" else ""
            if has_more:
                continue
        state.node_offset += 1
        state.after_thread_id = state.thread_revision = state.history_cursor = None
    if sessions.path_resolver.revision != snapshot.catalog_revision:
        raise ValueError("工作区消息搜索期间目录已更新，请重新查询")
    admission.check()
    complete = state.node_offset == len(snapshot.items)
    return WorkspaceMessageSearchResultsDTO(
        catalog_revision=snapshot.catalog_revision, items=items, complete=complete,
        limited_reason=reason, scanned_sessions=len(scanned_sessions), scanned_threads=scanned_threads,
        scanned_items=budget.scanned_items, scanned_bytes=budget.scanned_bytes,
        cursor=base64.urlsafe_b64encode(state.model_dump_json().encode()).decode().rstrip("=")
        if not complete and reason != "item_size_limit" else None,
    )
