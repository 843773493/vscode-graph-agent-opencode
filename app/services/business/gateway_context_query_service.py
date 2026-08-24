from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from typing import TypeVar

from app.abstractions.session_context import (
    WorkspaceSessionContextAccessError,
    WorkspaceSessionContextTransportProtocol,
)
from app.schemas.gateway import GatewayWorkspaceListDTO
from app.schemas.public_v2.session_context import (
    SessionContextItemDTO,
    SessionContextPartialErrorDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchMatchDTO,
    SessionContextSearchResultDTO,
)


_SEARCH_CONCURRENCY = 8
_PER_WORKSPACE_MATCH_LIMIT = 200
_ERROR_CHARS = 500
ResultDTO = TypeVar(
    "ResultDTO",
    SessionContextReadResultDTO,
    SessionContextSearchResultDTO,
)


class GatewayContextQueryService:
    """在调用侧编排 Gateway inventory 与跨工作区上下文查询。"""

    def __init__(
        self,
        *,
        transport: WorkspaceSessionContextTransportProtocol,
    ) -> None:
        self._transport = transport

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
        if request.resource != "boxteam://gateway/workspaces":
            raise WorkspaceSessionContextAccessError(
                "Gateway read 当前仅支持 boxteam://gateway/workspaces"
            )
        inventory = await self._transport.list_gateway_workspaces()
        revision = _workspace_inventory_revision(inventory)
        _require_revision(request.expected_revision, revision)
        offset = _decode_cursor(request.cursor, revision=revision)
        candidates = [
            SessionContextItemDTO(
                kind="workspace",
                locator=f"boxteam://workspace/{item.workspace_id}",
                data=item.model_dump(mode="json"),
            )
            for item in inventory.items[offset:offset + request.limit]
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
        if request.resource != "boxteam://gateway":
            raise WorkspaceSessionContextAccessError(
                "Gateway search 必须显式使用 boxteam://gateway"
            )
        inventory = await self._transport.list_gateway_workspaces()
        semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)

        async def search_one(workspace_id: str) -> SessionContextSearchResultDTO:
            workspace_request = request.model_copy(
                update={
                    "resource": f"boxteam://workspace/{workspace_id}/sessions",
                    "cursor": None,
                    "expected_revision": None,
                    "max_results": _PER_WORKSPACE_MATCH_LIMIT,
                    "max_chars": 65_536,
                }
            )
            async with semaphore:
                return await self._transport.search_context_in_workspace(
                    workspace_id,
                    workspace_request,
                )

        gathered = await asyncio.gather(
            *(search_one(item.workspace_id) for item in inventory.items),
            return_exceptions=True,
        )
        matches = []
        errors: list[SessionContextPartialErrorDTO] = []
        revisions: list[str] = []
        total_matches = 0
        upstream_truncated = False
        for workspace, response in zip(inventory.items, gathered, strict=True):
            resource = f"boxteam://workspace/{workspace.workspace_id}"
            if isinstance(response, BaseException):
                errors.append(
                    SessionContextPartialErrorDTO(
                        resource=resource,
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
                        resource=resource,
                        error="该工作区匹配数超过单次 fan-out 上限，结果已截断",
                    )
                )

        matches.sort(key=lambda item: (item.locator, item.match_start, item.match_end))
        revision = _content_revision(
            {
                "inventory_revision": _workspace_inventory_revision(inventory),
                "workspace_revisions": revisions,
                "failed_resources": [item.resource for item in errors],
                "query": request.query,
                "sources": request.sources,
                "match_mode": request.match_mode,
                "case_sensitive": request.case_sensitive,
            }
        )
        _require_revision(request.expected_revision, revision)
        offset = _decode_cursor(request.cursor, revision=revision)
        selected = matches[offset:offset + request.max_results]
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
            raise ValueError(
                "max_chars 太小，无法容纳 Gateway search 的首个 match"
            )
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
        resource=request.resource,
        view="inventory",
        revision=revision,
        truncated=has_more,
        has_more=has_more,
        next_cursor=(
            _encode_cursor(offset + len(items), revision=revision)
            if has_more
            else None
        ),
        items=items,
    )
    return _set_exact_returned_chars(result)


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
        resource=request.resource,
        query=request.query,
        match_mode=request.match_mode,
        revision=revision,
        truncated=has_more or upstream_truncated or omitted_error_count > 0,
        has_more=has_more,
        next_cursor=(
            _encode_cursor(offset + len(matches), revision=revision)
            if has_more
            else None
        ),
        total_matches=total_matches,
        matches=matches,
        partial_errors=errors,
        omitted_partial_error_count=omitted_error_count,
    )
    return _set_exact_returned_chars(result)


def _set_exact_returned_chars(
    result: ResultDTO,
) -> ResultDTO:
    while True:
        serialized_chars = len(result.model_dump_json())
        if result.returned_chars == serialized_chars:
            break
        result.returned_chars = serialized_chars
    return result


def _require_within_budget(returned_chars: int, max_chars: int) -> None:
    if returned_chars > max_chars:
        raise ValueError(
            "max_chars 太小，无法容纳 Gateway context 的基础响应 envelope"
        )


def _workspace_inventory_revision(inventory: GatewayWorkspaceListDTO) -> str:
    return _content_revision(
        {
            "active_workspace_id": inventory.active_workspace_id,
            "items": [
                {
                    "workspace_id": item.workspace_id,
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


def _require_revision(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise WorkspaceSessionContextAccessError(
            f"Gateway context revision 已变化: expected={expected}, actual={actual}"
        )


def _encode_cursor(offset: int, *, revision: str) -> str:
    payload = json.dumps(
        {"offset": offset, "revision": revision},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, *, revision: str) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
        raise ValueError("Gateway context cursor 格式无效") from error
    if not isinstance(payload, dict) or payload.get("revision") != revision:
        raise WorkspaceSessionContextAccessError(
            "Gateway context revision 已变化，请从第一页重新读取"
        )
    offset = payload.get("offset")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("Gateway context cursor offset 无效")
    return offset
