"""原 diagnostics 来源的正文查询和定位；工作区只筛选关联进程，不拥有日志正文。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.diagnostics import (
    _MAX_GATEWAY_LOG_ENTRIES,
    _candidate_gateway_logs,
    _LogCandidate,
    _read_log,
    _workspace_candidates,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.log_query import (
    LogFileObservation,
    LogQueryBudget,
    LogQueryLimit,
    LogRecordReference,
    RuntimeLogQueryError,
    query_runtime_log,
    read_runtime_log_record,
)
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogLocateRequest,
    GatewayDiagnosticLogLocationDTO,
    GatewayDiagnosticLogMatchDTO,
    GatewayDiagnosticLogQueryRequest,
    GatewayDiagnosticLogQueryResultDTO,
    GatewayDiagnosticLogQueryScopeDTO,
    GatewayDiagnosticLogQuerySourceDTO,
    GatewayDiagnosticLogReferenceDTO,
)

MAX_QUERY_SOURCES = 64


@dataclass(frozen=True, slots=True)
class _Source:
    candidate: _LogCandidate
    workspace_ids: tuple[str, ...]

    def describe(self) -> GatewayDiagnosticLogQuerySourceDTO:
        log = _read_log(self.candidate, 300, include_tail=False)
        return GatewayDiagnosticLogQuerySourceDTO(
            log_id=log.log_id, source=log.source, service=log.service,
            label=log.label, workspace_ids=list(self.workspace_ids), status=log.status, error=log.error,
        )


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    gateway_id: str
    scope: GatewayDiagnosticLogQueryScopeDTO
    query: str
    sources_version: str
    source_offset: int = Field(default=0, ge=0, strict=True)
    file: LogFileObservation | None = None
    line_offset: int = Field(default=0, ge=0, strict=True)
    match_offset: int = Field(default=0, ge=0, strict=True)
    unavailable: bool = False


def _sources(
    registry: GatewayWorkspaceRegistry, scope: GatewayDiagnosticLogQueryScopeDTO,
) -> tuple[list[_Source], str, bool]:
    targets = sorted((target for target in registry.targets() if target.connection_kind == "local"),
                     key=lambda target: target.workspace_id)
    known_workspaces = {target.workspace_id for target in targets}
    if any(workspace_id not in known_workspaces for workspace_id in scope.workspace_ids):
        raise LookupError("运行日志范围包含未登记的本机工作区")
    candidates = [candidate for target in targets for candidate in _workspace_candidates(target, registry)]
    grouped: dict[Path | str, list[_LogCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.log_path or candidate.log_id, []).append(candidate)
    source_limit = False
    if scope.source == "gateway":
        candidates = _candidate_gateway_logs(
            {candidate.log_path.name for candidate in candidates if candidate.log_path is not None},
            max_entries=_MAX_GATEWAY_LOG_ENTRIES + 1,
        )
        source_limit = len(candidates) > _MAX_GATEWAY_LOG_ENTRIES + 1
        sources = [_Source(candidate, ()) for candidate in candidates[:_MAX_GATEWAY_LOG_ENTRIES + 1]]
    else:
        sources = [
            _Source(group[0], tuple(dict.fromkeys(item.workspace_id for item in group if item.workspace_id is not None)))
            for group in grouped.values()
            if any(item.workspace_id in scope.workspace_ids for item in group)
        ]
    sources.sort(key=lambda source: source.candidate.log_id)
    # 仅摘要来源定位与关联事实；不读取正文，也不把本机路径放入 cursor。
    version = hashlib.sha256(json.dumps([
        (source.candidate.log_id, str(source.candidate.log_path), source.workspace_ids) for source in sources
    ], ensure_ascii=False).encode()).hexdigest()
    return sources, version, source_limit


def _state(request: GatewayDiagnosticLogQueryRequest, gateway_id: str, sources_version: str) -> _Cursor:
    current = _Cursor(gateway_id=gateway_id, scope=request.scope, query=request.query, sources_version=sources_version)
    if request.cursor is None:
        return current
    try:
        raw = base64.b64decode(request.cursor + "=" * (-len(request.cursor) % 4), altchars=b"-_", validate=True)
        previous = _Cursor.model_validate_json(raw)
    except (ValueError, binascii.Error, ValidationError) as error:
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "malformed", "运行日志 cursor 格式无效") from error
    if previous.gateway_id != gateway_id:
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "owner_mismatch", "运行日志 cursor 属于其它 Gateway")
    if previous.scope != request.scope:
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "scope_mismatch", "运行日志 cursor 的来源范围不符")
    if previous.query != request.query:
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "query_mismatch", "运行日志 cursor 的查询不符")
    if previous.sources_version != sources_version:
        raise RuntimeLogQueryError("stale_runtime_log_cursor", "sources_changed", "运行日志来源或工作区关联已变化")
    return previous


def _query(
    request: GatewayDiagnosticLogQueryRequest, gateway_id: str,
    sources: list[_Source], sources_version: str, source_limit: bool,
) -> GatewayDiagnosticLogQueryResultDTO:
    state = _state(request, gateway_id, sources_version)
    if state.source_offset > len(sources) or state.source_offset == len(sources) and (
        state.file is not None or state.line_offset or state.match_offset
    ):
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "position_out_of_bounds", "运行日志来源续点越界")
    if state.file is None and (state.line_offset or state.match_offset):
        raise RuntimeLogQueryError("invalid_runtime_log_cursor", "malformed", "运行日志续点缺少当前文件观察版本")
    budget = LogQueryBudget.create(request.deadline_at)
    items: list[GatewayDiagnosticLogMatchDTO] = []
    descriptions: list[GatewayDiagnosticLogQuerySourceDTO] = []
    reason: LogQueryLimit | Literal["source_limit", "source_unavailable"] | None = None
    while state.source_offset < len(sources):
        reason = budget.limited_reason
        if reason is not None:
            break
        if len(descriptions) == MAX_QUERY_SOURCES or len(items) == request.limit:
            reason = "scan_limit" if len(descriptions) == MAX_QUERY_SOURCES else "result_limit"
            break
        source = sources[state.source_offset]
        description = source.describe()
        descriptions.append(description)
        state.unavailable = state.unavailable or description.status == "unavailable"
        path = source.candidate.log_path
        if path is not None and description.status != "unavailable":
            try:
                page = query_runtime_log(path, request.query, limit=request.limit - len(items), budget=budget,
                    offset=state.line_offset, match_offset=state.match_offset, observation=state.file)
            except FileNotFoundError as error:
                raise RuntimeLogQueryError("stale_runtime_log_cursor", "file_deleted", "运行日志在查询时已删除") from error
            for hit in page.items:
                ref = hit.reference
                items.append(GatewayDiagnosticLogMatchDTO(source=description, reference=GatewayDiagnosticLogReferenceDTO(
                    gateway_id=gateway_id, log_id=description.log_id, device=ref.device, inode=ref.inode,
                    line_start=ref.offset, line_end=ref.offset + ref.length, content_version=ref.content_version,
                    start_codepoint=ref.start, end_codepoint=ref.end,
                ), preview=hit.preview, preview_start_codepoint=hit.preview_start))
            if page.limited_reason is not None:
                state.file, state.line_offset, state.match_offset = page.observation, page.offset, page.match_offset
                reason = page.limited_reason
                break
        elif state.file is not None:
            raise RuntimeLogQueryError("stale_runtime_log_cursor", "file_deleted", "续查的运行日志文件已不可用")
        state.source_offset += 1
        state.file, state.line_offset, state.match_offset = None, 0, 0
    exhausted = state.source_offset == len(sources)
    if exhausted and source_limit:
        reason = "source_limit"
    elif exhausted and state.unavailable:
        reason = "source_unavailable"
    return GatewayDiagnosticLogQueryResultDTO(
        gateway_id=gateway_id, scope=request.scope, query=request.query, items=items, sources=descriptions,
        complete=exhausted and not source_limit and not state.unavailable, limited_reason=reason,
        cursor=base64.urlsafe_b64encode(state.model_dump_json().encode()).decode().rstrip("=")
        if not exhausted and reason != "item_size_limit" else None,
        scanned_sources=len(descriptions), scanned_lines=budget.lines, scanned_bytes=budget.bytes_read,
    )


async def query_diagnostic_logs(
    registry: GatewayWorkspaceRegistry, request: GatewayDiagnosticLogQueryRequest, *, gateway_id: str,
) -> GatewayDiagnosticLogQueryResultDTO:
    sources, version, limited = _sources(registry, request.scope)
    result = await run_sync_shielded(_query, request, gateway_id, sources, version, limited)
    if _sources(registry, request.scope)[1] != version:
        raise RuntimeLogQueryError("stale_runtime_log_cursor", "sources_changed", "查询期间运行日志来源已变化")
    return result


async def locate_diagnostic_log(
    registry: GatewayWorkspaceRegistry, request: GatewayDiagnosticLogLocateRequest, *, gateway_id: str,
) -> GatewayDiagnosticLogLocationDTO:
    reference = request.reference
    if reference.gateway_id != gateway_id:
        raise RuntimeLogQueryError("stale_runtime_log_reference", "owner_mismatch", "运行日志命中属于其它 Gateway")
    sources, _, _ = _sources(registry, request.scope)
    source = next((item for item in sources if item.candidate.log_id == reference.log_id), None)
    if source is None or source.candidate.log_path is None:
        raise RuntimeLogQueryError("stale_runtime_log_reference", "source_unavailable", "运行日志来源已移除或不属于明确范围")
    text = await run_sync_shielded(read_runtime_log_record, source.candidate.log_path, LogRecordReference(
        reference.device, reference.inode, reference.line_start, reference.line_end - reference.line_start,
        reference.content_version, reference.start_codepoint, reference.end_codepoint,
    ))
    return GatewayDiagnosticLogLocationDTO(source=source.describe(), reference=reference, text=text)
