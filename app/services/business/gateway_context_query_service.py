from __future__ import annotations

import hashlib
import json

from app.abstractions.resource_search import GatewayResourceSearchProtocol
from app.abstractions.session_context import (
    WorkspaceSessionContextAccessError,
    WorkspaceSessionContextTransportProtocol,
)
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.gateway.workspaces import GatewayWorkspaceDTO, GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import (
    GatewayContextSearchRequest,
    GatewayContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    ParsedSessionContextResource,
    SessionContextCursorCodec,
    exact_returned_chars_with_appended_json,
    gateway_session_inventory_identity,
    parse_session_context_ref,
    require_cursor_offset_in_range,
    require_session_context_revision,
    session_context_read_operation,
    set_exact_returned_chars,
    validate_session_context_read_view,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)


class GatewayContextQueryService:
    """读取 Gateway inventory；跨目标搜索仅转交 Gateway 的统一调度。"""

    def __init__(
        self,
        *,
        transport: WorkspaceSessionContextTransportProtocol,
        resource_search: GatewayResourceSearchProtocol | None = None,
    ) -> None:
        self._transport = transport
        self._resource_search = resource_search

    @property
    def resource_search(self) -> GatewayResourceSearchProtocol:
        if self._resource_search is None:
            raise RuntimeError("当前组合根未装配 Gateway 联邦资源搜索客户端")
        return self._resource_search

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        return await self._transport.list_gateway_workspaces()

    async def read_resource_context(
        self, request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return await self._transport.read_resource_context(request)

    async def search_resource_context(
        self, request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        return await self._transport.search_resource_context(request)

    async def read_gateway_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        resource = require_gateway_inventory(
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
                uri=workspace_session_inventory_uri(_backend_workspace_id(item), gateway_authority=_workspace_gateway_authority(item, resource)),
                scope="workspace",
                data=_workspace_public_data(item),
            )
            for item in inventory.items[offset : offset + request.limit]
        ]
        accepted: list[SessionContextItemDTO] = []
        accepted_item_json_chars = 0
        for item in candidates:
            serialized_item = item.model_dump_json()
            candidate_returned_item_count = len(accepted) + 1
            empty_candidate = _build_gateway_read_result(
                request=request,
                revision=revision,
                items=[],
                returned_item_count=candidate_returned_item_count,
                offset=offset,
                total_items=len(inventory.items),
            )
            candidate_returned_chars = exact_returned_chars_with_appended_json(
                empty_result_returned_chars=empty_candidate.returned_chars,
                appended_json_chars=(
                    accepted_item_json_chars + len(serialized_item) + len(accepted)
                ),
            )
            if candidate_returned_chars > request.max_chars:
                break
            accepted.append(item)
            accepted_item_json_chars += len(serialized_item)
        if candidates and not accepted:
            raise ValueError(
                "max_chars 太小，无法容纳 Gateway workspace inventory 的首个 item"
            )
        result = _build_gateway_read_result(
            request=request,
            revision=revision,
            items=accepted,
            returned_item_count=len(accepted),
            offset=offset,
            total_items=len(inventory.items),
        )
        _require_within_budget(result.returned_chars, request.max_chars)
        return result

    async def search_gateway_context(
        self, request: GatewayContextSearchRequest,
    ) -> GatewayContextSearchResultDTO:
        return await self._transport.search_gateway_context(request)


def _build_gateway_read_result(
    *,
    request: SessionContextReadRequest,
    revision: str,
    items: list[SessionContextItemDTO],
    returned_item_count: int,
    offset: int,
    total_items: int,
) -> SessionContextReadResultDTO:
    has_more = offset + returned_item_count < total_items
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
                offset=offset + returned_item_count,
            )
            if has_more
            else None
        ),
        items=items,
    )
    return set_exact_returned_chars(result, operation="gateway read_context")


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


def require_gateway_inventory(
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
        or resource.gateway_authority not in {None, gateway_id}
    ):
        raise WorkspaceSessionContextAccessError(
            "Gateway session inventory resource_identity/URI 与当前真实 Gateway 身份不匹配"
        )
    return resource


def _workspace_gateway_authority(
    item: GatewayWorkspaceDTO, inventory: ParsedSessionContextResource,
) -> str | None:
    if item.connection_kind == "remote_gateway":
        if item.remote is None:
            raise WorkspaceSessionContextAccessError(
                f"远端工作区缺少所属 Gateway 身份: workspace_id={item.workspace_id}"
            )
        return item.remote.gateway_id
    return inventory.gateway_authority


def _backend_workspace_id(item: GatewayWorkspaceDTO) -> str:
    backend_workspace_id = item.backend_workspace_id
    if not isinstance(backend_workspace_id, str) or not backend_workspace_id:
        raise WorkspaceSessionContextAccessError(
            "Gateway workspace 缺少真实 backend_workspace_id，无法构造 Workspace URI: "
            f"workspace_id={item.workspace_id}"
        )
    return backend_workspace_id


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
