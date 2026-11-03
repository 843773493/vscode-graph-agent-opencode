"""在原 catalog 与 Session 读准入内编排请求日志查询，不建立搜索副本。"""

import asyncio
import base64
import binascii
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.resource_uri import resource_display_uri
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.llm_request_log import (
    WorkspaceLLMRequestLogMatchDTO,
    WorkspaceLLMRequestLogSearchResultsDTO,
)
from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.llm_request_log_service import (
    LLMRequestLogError,
    LLMRequestLogService,
    LogSearchBudget,
    LogSearchPosition,
)

MAX_SCANNED_NODES = 64


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    gateway_id: str
    workspace_id: str
    query: str
    catalog_revision: int
    node_offset: int = Field(default=0, ge=0)
    position: LogSearchPosition | None = None


async def search_workspace_llm_requests(
    catalog: SessionCatalogReadService, sessions: SessionService, logs: LLMRequestLogService,
    request: WorkspaceSearchRequest, *, gateway_id: str,
) -> WorkspaceLLMRequestLogSearchResultsDTO:
    admission = NameSearchBudget.create(request.deadline_at)
    snapshot = await catalog.export_index(check=admission.check)
    state = _Cursor(gateway_id=gateway_id, workspace_id=sessions.workspace_id,
                    query=request.query, catalog_revision=snapshot.catalog_revision)
    if request.cursor is not None:
        try:
            raw = base64.b64decode(request.cursor + "=" * (-len(request.cursor) % 4), altchars=b"-_", validate=True)
            previous = _Cursor.model_validate_json(raw)
        except (ValueError, binascii.Error, ValidationError) as error:
            raise LLMRequestLogError("invalid_request_log_cursor", "malformed", "请求日志搜索 cursor 格式无效") from error
        if (previous.gateway_id, previous.workspace_id) != (state.gateway_id, state.workspace_id):
            raise LLMRequestLogError("invalid_request_log_cursor", "owner_mismatch", "请求日志搜索 cursor 的归属不符，请重新查询")
        if previous.query != state.query:
            raise LLMRequestLogError("invalid_request_log_cursor", "query_mismatch", "请求日志搜索 cursor 的查询不符，请重新查询")
        if previous.catalog_revision != state.catalog_revision:
            raise LLMRequestLogError("stale_request_log_cursor", "catalog_changed", "请求日志搜索 cursor 的目录版本不符，请重新查询")
        state = previous
    if state.node_offset > len(snapshot.items):
        raise LLMRequestLogError("invalid_request_log_cursor", "position_out_of_bounds", "请求日志搜索 cursor 目录位置越界")
    budget = LogSearchBudget(check=admission.check)
    items: list[WorkspaceLLMRequestLogMatchDTO] = []
    scanned_nodes = 0
    reason = None
    continuable = True
    while state.node_offset < len(snapshot.items):
        reason = budget.limited_reason()
        if reason:
            break
        if scanned_nodes >= MAX_SCANNED_NODES or len(items) >= request.limit:
            reason = "scan_limit" if scanned_nodes >= MAX_SCANNED_NODES else "result_limit"
            break
        node = snapshot.items[state.node_offset]
        scanned_nodes += 1
        if node.session is not None:
            async with asyncio.timeout(admission.remaining()):
                async with sessions.session_read_admission(node.node_id):
                    page = await run_sync_shielded(logs.search_session_logs, node.node_id, request.query,
                        limit=request.limit - len(items), position=state.position, budget=budget)
            uri = resource_display_uri(scope="workspace", scope_id=sessions.workspace_id, kind="session",
                                       tail_segments=(node.node_id,), gateway_authority=gateway_id)
            items.extend(WorkspaceLLMRequestLogMatchDTO(session_uri=uri, session_title=node.name, match=hit)
                         for hit in page.items)
            if not page.complete:
                if page.position is not None:
                    state.position = page.position
                # 目录过大／单条超限无法给出等价续点；重新查询前须缩小来源或处理原日志。
                continuable = page.position is not None or page.limited_reason == "deadline"
                reason = page.limited_reason
                break
        state.node_offset += 1
        state.position = None
    if sessions.path_resolver.revision != snapshot.catalog_revision:
        raise LLMRequestLogError("stale_request_log_cursor", "catalog_changed", "请求日志搜索期间目录已更新，请重新查询")
    admission.check()
    complete = state.node_offset == len(snapshot.items)
    return WorkspaceLLMRequestLogSearchResultsDTO(
        catalog_revision=snapshot.catalog_revision, items=items, complete=complete, limited_reason=reason,
        scanned_nodes=scanned_nodes, scanned_records=budget.scanned_records, scanned_bytes=budget.scanned_bytes,
        cursor=base64.urlsafe_b64encode(state.model_dump_json().encode()).decode().rstrip("=")
        if not complete and continuable else None,
    )
