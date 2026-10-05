from __future__ import annotations

import asyncio
import hashlib
import json

from app.abstractions.session_context import (
    WorkspaceSessionContextAccessError,
    WorkspaceSessionContextTransportProtocol,
)
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.gateway import GatewayWorkspaceDTO, GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextPartialErrorDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchMatchDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    ParsedSessionContextResource,
    SessionContextCursorCodec,
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    parse_session_context_ref,
    require_cursor_offset_in_range,
    require_session_context_revision,
    session_context_read_operation,
    session_resource_identity,
    set_exact_returned_chars,
    validate_session_context_read_view,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)

_SEARCH_CONCURRENCY = 8
_PER_WORKSPACE_MATCH_LIMIT = 200
_ERROR_CHARS = 500


class GatewayContextQueryService:
    """在调用侧编排 Gateway inventory 与跨工作区上下文查询。"""

    def __init__(
        self,
        *,
        transport: WorkspaceSessionContextTransportProtocol,
    ) -> None:
        self._transport = transport

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        return await self._transport.list_gateway_workspaces()

    async def read_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return await self._transport.read_context_in_workspace(workspace_id, request)

    async def search_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        return await self._transport.search_context_in_workspace(workspace_id, request)

    async def read_gateway_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        resource = _require_gateway_inventory(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
        )
        validate_session_context_read_view(
            resource,
            request.view,
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )
        inventory = await self._transport.list_gateway_workspaces()
        revision = _workspace_inventory_revision(inventory)
        require_session_context_revision(request.revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            revision=revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
        )
        if char_offset:
            raise ValueError("Gateway session inventory 不支持 item 内字符偏移")
        require_cursor_offset_in_range(
            offset,
            len(inventory.items),
            uri=request.uri,
        )
        candidates = [
            SessionContextItemDTO(
                kind="workspace",
                resource_identity=workspace_session_inventory_identity(
                    _backend_workspace_id(item)
                ),
                uri=workspace_session_inventory_uri(_backend_workspace_id(item)),
                scope="workspace",
                data=_workspace_public_data(item),
            )
            for item in inventory.items[offset : offset + request.limit]
        ]
        accepted: list[SessionContextItemDTO] = []
        result = _build_gateway_read_result(
            request=request,
            revision=revision,
            items=accepted,
            offset=offset,
            total_items=len(inventory.items),
        )
        for item in candidates:
            candidate = _build_gateway_read_result(
                request=request,
                revision=revision,
                items=[*accepted, item],
                offset=offset,
                total_items=len(inventory.items),
            )
            if candidate.returned_chars > request.max_chars:
                break
            accepted.append(item)
            result = candidate
        if candidates and not accepted:
            raise ValueError(
                "max_chars 太小，无法容纳 Gateway workspace inventory 的首个 item"
            )
        _require_within_budget(result.returned_chars, request.max_chars)
        return result

    async def search_gateway_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        _require_gateway_inventory(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
        )
        inventory = await self._transport.list_gateway_workspaces()
        semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)
        targets = [(item, _backend_workspace_id(item)) for item in inventory.items]

        async def search_one(
            workspace_id: str,
            backend_workspace_id: str,
        ) -> SessionContextSearchResultDTO:
            workspace_request = request.model_copy(
                update={
                    "resource_identity": str(
                        workspace_session_inventory_identity(backend_workspace_id)
                    ),
                    "uri": workspace_session_inventory_uri(backend_workspace_id),
                    "scope": "workspace",
                    "cursor": None,
                    "revision": None,
                    "max_results": _PER_WORKSPACE_MATCH_LIMIT,
                    "max_chars": 65_536,
                }
            )
            async with semaphore:
                response = await self._transport.search_context_in_workspace(
                    workspace_id,
                    workspace_request,
                )
            _validate_workspace_search_result(
                response,
                request=workspace_request,
                backend_workspace_id=backend_workspace_id,
            )
            return response

        gathered = await asyncio.gather(
            *(
                search_one(item.workspace_id, backend_workspace_id)
                for item, backend_workspace_id in targets
            ),
            return_exceptions=True,
        )
        matches = []
        errors: list[SessionContextPartialErrorDTO] = []
        revisions: list[str] = []
        total_matches = 0
        upstream_truncated = False
        for (workspace, backend_workspace_id), response in zip(
            targets,
            gathered,
            strict=True,
        ):
            resource_identity = workspace_session_inventory_identity(
                backend_workspace_id
            )
            if isinstance(response, BaseException):
                errors.append(
                    SessionContextPartialErrorDTO(
                        resource_identity=resource_identity,
                        error=f"{type(response).__name__}: {response}"[:_ERROR_CHARS],
                    )
                )
                continue
            revisions.append(response.revision)
            total_matches += response.total_matches
            matches.extend(response.matches)
            if response.has_more:
                upstream_truncated = True
                errors.append(
                    SessionContextPartialErrorDTO(
                        resource_identity=resource_identity,
                        error="该工作区匹配数超过单次 fan-out 上限，结果已截断",
                    )
                )

        matches.sort(key=lambda item: (item.uri, item.match_start, item.match_end))
        revision = _content_revision(
            {
                "inventory_revision": _workspace_inventory_revision(inventory),
                "workspace_revisions": revisions,
                "failed_resource_identities": [
                    item.resource_identity for item in errors
                ],
                "query": request.query,
                "sources": request.sources,
                "match_mode": request.match_mode,
                "case_sensitive": request.case_sensitive,
            }
        )
        require_session_context_revision(request.revision, revision)
        query_hash = _search_query_hash(request)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            revision=revision,
            operation=f"search:{query_hash}",
        )
        if char_offset:
            raise ValueError("Gateway context search 不支持 item 内字符偏移")
        require_cursor_offset_in_range(offset, len(matches), uri=request.uri)
        selected = matches[offset : offset + request.max_results]
        accepted_matches: list[SessionContextSearchMatchDTO] = []
        accepted_errors: list[SessionContextPartialErrorDTO] = []
        result = _build_gateway_search_result(
            request=request,
            revision=revision,
            matches=accepted_matches,
            errors=accepted_errors,
            total_error_count=len(errors),
            total_matches=total_matches,
            available_match_count=len(matches),
            offset=offset,
            upstream_truncated=upstream_truncated,
        )
        for match in selected:
            candidate = _build_gateway_search_result(
                request=request,
                revision=revision,
                matches=[*accepted_matches, match],
                errors=accepted_errors,
                total_error_count=len(errors),
                total_matches=total_matches,
                available_match_count=len(matches),
                offset=offset,
                upstream_truncated=upstream_truncated,
            )
            if candidate.returned_chars > request.max_chars:
                break
            accepted_matches.append(match)
            result = candidate
        if selected and not accepted_matches:
            raise ValueError("max_chars 太小，无法容纳 Gateway search 的首个 match")
        for error in errors:
            candidate = _build_gateway_search_result(
                request=request,
                revision=revision,
                matches=accepted_matches,
                errors=[*accepted_errors, error],
                total_error_count=len(errors),
                total_matches=total_matches,
                available_match_count=len(matches),
                offset=offset,
                upstream_truncated=upstream_truncated,
            )
            if candidate.returned_chars > request.max_chars:
                break
            accepted_errors.append(error)
            result = candidate
        _require_within_budget(result.returned_chars, request.max_chars)
        return result


def _build_gateway_read_result(
    *,
    request: SessionContextReadRequest,
    revision: str,
    items: list[SessionContextItemDTO],
    offset: int,
    total_items: int,
) -> SessionContextReadResultDTO:
    has_more = offset + len(items) < total_items
    result = SessionContextReadResultDTO(
        resource_identity=request.resource_identity,
        uri=request.uri,
        scope=request.scope,
        view=request.view,
        record_index=request.record_index,
        assembly_ref=request.assembly_ref,
        revision=revision,
        truncated=has_more,
        has_more=has_more,
        next_cursor=(
            SessionContextCursorCodec.encode(
                resource_identity=request.resource_identity,
                uri=request.uri,
                scope=request.scope,
                revision=revision,
                operation=session_context_read_operation(
                    view=request.view,
                    include=request.include,
                    recent_rounds=request.recent_rounds,
                    include_initial_goal=request.include_initial_goal,
                ),
                record_index=request.record_index,
                assembly_ref=request.assembly_ref,
                offset=offset + len(items),
            )
            if has_more
            else None
        ),
        items=items,
    )
    return set_exact_returned_chars(result, operation="gateway read_context")


def _build_gateway_search_result(
    *,
    request: SessionContextSearchRequest,
    revision: str,
    matches: list[SessionContextSearchMatchDTO],
    errors: list[SessionContextPartialErrorDTO],
    total_error_count: int,
    total_matches: int,
    available_match_count: int,
    offset: int,
    upstream_truncated: bool,
) -> SessionContextSearchResultDTO:
    has_more = offset + len(matches) < available_match_count
    omitted_error_count = total_error_count - len(errors)
    result = SessionContextSearchResultDTO(
        resource_identity=request.resource_identity,
        uri=request.uri,
        scope=request.scope,
        query=request.query,
        match_mode=request.match_mode,
        revision=revision,
        truncated=has_more or upstream_truncated or omitted_error_count > 0,
        has_more=has_more,
        next_cursor=(
            SessionContextCursorCodec.encode(
                resource_identity=request.resource_identity,
                uri=request.uri,
                scope=request.scope,
                revision=revision,
                operation=f"search:{_search_query_hash(request)}",
                offset=offset + len(matches),
            )
            if has_more
            else None
        ),
        total_matches=total_matches,
        matches=matches,
        partial_errors=errors,
        omitted_partial_error_count=omitted_error_count,
    )
    return set_exact_returned_chars(result, operation="gateway search_context")


def _require_within_budget(returned_chars: int, max_chars: int) -> None:
    if returned_chars > max_chars:
        raise ValueError("max_chars 太小，无法容纳 Gateway context 的基础响应 envelope")


def _workspace_inventory_revision(inventory: GatewayWorkspaceListDTO) -> str:
    return _content_revision(
        {
            "active_workspace_id": inventory.active_workspace_id,
            "items": [
                {
                    "workspace_id": item.workspace_id,
                    "backend_workspace_id": item.backend_workspace_id,
                    "parent_workspace_id": item.parent_workspace_id,
                    "name": item.name,
                    "root_path": item.root_path,
                    "backend_url": item.backend_url,
                    "connection_kind": item.connection_kind,
                    "active": item.active,
                    "managed": item.managed,
                    "removable": item.removable,
                    "system_default": item.system_default,
                    "remote": (
                        item.remote.model_dump(mode="json")
                        if item.remote is not None
                        else None
                    ),
                    "service_names": sorted(item.services),
                }
                for item in inventory.items
            ],
        }
    )


def _content_revision(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _require_gateway_inventory(
    *,
    resource_identity: str,
    uri: str,
    scope: str,
) -> ParsedSessionContextResource:
    resource = parse_session_context_ref(
        resource_identity=resource_identity,
        uri=uri,
        scope=scope,
    )
    gateway_id = require_current_gateway_id("gateway session inventory")
    if (
        resource.scope != "gateway"
        or resource.scope_id != gateway_id
        or resource.kind != "workspace_sessions"
        or resource.resource_identity != gateway_session_inventory_identity(gateway_id)
        or resource.uri != gateway_session_inventory_uri(gateway_id)
    ):
        raise WorkspaceSessionContextAccessError(
            "Gateway session inventory resource_identity/URI 与当前真实 Gateway 身份不匹配"
        )
    return resource


def _backend_workspace_id(item: GatewayWorkspaceDTO) -> str:
    backend_workspace_id = item.backend_workspace_id
    if not isinstance(backend_workspace_id, str) or not backend_workspace_id:
        raise WorkspaceSessionContextAccessError(
            "Gateway workspace 缺少真实 backend_workspace_id，无法构造 Workspace URI: "
            f"workspace_id={item.workspace_id}"
        )
    return backend_workspace_id


def _validate_workspace_search_result(
    result: SessionContextSearchResultDTO,
    *,
    request: SessionContextSearchRequest,
    backend_workspace_id: str,
) -> None:
    if (
        result.resource_identity != request.resource_identity
        or result.uri != request.uri
        or result.scope != request.scope
    ):
        raise WorkspaceSessionContextAccessError(
            "Workspace context search 响应资源身份与请求不一致: "
            f"workspace_scope_id={backend_workspace_id}"
        )
    for match in result.matches:
        resource = parse_session_context_ref(
            resource_identity=match.resource_identity,
            uri=match.uri,
            scope=match.scope,
        )
        if (
            resource.scope != "workspace"
            or resource.scope_id != backend_workspace_id
            or resource.kind != "session"
            or resource.session_id is None
            or resource.resource_identity
            != session_resource_identity(backend_workspace_id, resource.session_id)
        ):
            raise WorkspaceSessionContextAccessError(
                "Workspace context search 返回了其他 Workspace 的资源身份: "
                f"workspace_scope_id={backend_workspace_id}, uri={match.uri}"
            )


def _workspace_public_data(item: GatewayWorkspaceDTO) -> dict[str, object]:
    return {
        "workspace_id": item.workspace_id,
        "backend_workspace_id": _backend_workspace_id(item),
        "name": item.name,
        "connection_kind": item.connection_kind,
        "status": item.status,
        "active": item.active,
        "managed": item.managed,
    }


def _search_query_hash(request: SessionContextSearchRequest) -> str:
    return _content_revision(
        {
            "query": request.query,
            "sources": request.sources,
            "match_mode": request.match_mode,
            "case_sensitive": request.case_sensitive,
        }
    )
