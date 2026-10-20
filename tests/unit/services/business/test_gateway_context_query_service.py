from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest

from app.abstractions.session_context import (
    SessionContextRevisionChangedError,
    WorkspaceSessionContextAccessError,
)
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.gateway.workspaces import (
    GatewayRemoteConnectionSummaryDTO,
    GatewayWorkspaceDTO,
    GatewayWorkspaceListDTO,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchMatchDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    parse_session_context_ref,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

GATEWAY_ID = "gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _current_gateway_identity() -> Generator[None, None, None]:
    token = set_current_gateway_id(GATEWAY_ID)
    yield
    reset_current_gateway_id(token)


def _workspace_id(index: int) -> str:
    return f"gateway_workspace_{index}"


def _backend_workspace_id(index: int) -> str:
    return str(UUID(int=index + 1))


def _workspace(
    workspace_id: str,
    *,
    checked_at: str,
    name: str | None = None,
) -> GatewayWorkspaceDTO:
    index = int(workspace_id.rsplit("_", maxsplit=1)[-1])
    return GatewayWorkspaceDTO(
        workspace_id=workspace_id,
        backend_workspace_id=_backend_workspace_id(index),
        name=name or workspace_id,
        backend_url=f"http://127.0.0.1/{workspace_id}",
        connection_kind="local",
        status="ready",
        runtime_action="probe_external_backend",
        checked_at=checked_at,
    )


def _gateway_read_request(**kwargs: object) -> SessionContextReadRequest:
    return SessionContextReadRequest(
        resource_identity=gateway_session_inventory_identity(GATEWAY_ID),
        uri=gateway_session_inventory_uri(GATEWAY_ID),
        scope="gateway",
        **kwargs,
    )


def _gateway_search_request(**kwargs: object) -> SessionContextSearchRequest:
    return SessionContextSearchRequest(
        resource_identity=gateway_session_inventory_identity(GATEWAY_ID),
        uri=gateway_session_inventory_uri(GATEWAY_ID),
        scope="gateway",
        **kwargs,
    )


def _workspace_search_result(
    workspace_id: str,
    *,
    query: str,
    total_matches: int = 0,
    matches: list[SessionContextSearchMatchDTO] | None = None,
    has_more: bool = False,
    truncated: bool = False,
    next_cursor: str | None = None,
) -> SessionContextSearchResultDTO:
    index = int(workspace_id.rsplit("_", maxsplit=1)[-1])
    backend_workspace_id = _backend_workspace_id(index)
    return SessionContextSearchResultDTO(
        resource_identity=workspace_session_inventory_identity(backend_workspace_id),
        uri=workspace_session_inventory_uri(backend_workspace_id),
        scope="workspace",
        query=query,
        match_mode="literal",
        revision=f"rev-{workspace_id}",
        total_matches=total_matches,
        matches=matches or [],
        has_more=has_more,
        truncated=truncated,
        next_cursor=next_cursor,
    )


def _session_match(
    backend_workspace_id: str,
    session_id: str,
    *,
    preview: str = "marker",
) -> SessionContextSearchMatchDTO:
    return SessionContextSearchMatchDTO(
        resource_identity=session_resource_identity(backend_workspace_id, session_id),
        uri=session_resource_uri(backend_workspace_id, session_id),
        scope="workspace",
        preview=preview,
        source="effective_context",
        revision=f"rev-{session_id}",
        record_index=0,
        match_start=0,
        match_end=6,
    )


class _FakeTransport:
    def __init__(self, workspace_ids: list[str]) -> None:
        self.workspace_ids = workspace_ids
        self.workspace_names: dict[str, str] = {}
        self.remote_workspaces: dict[str, GatewayRemoteConnectionSummaryDTO] = {}
        self.backend_workspace_ids: dict[str, str] = {}
        self.missing_backend_workspace_ids: set[str] = set()
        self.inventory_calls = 0
        self.visited: list[str] = []
        self.search_requests: dict[str, SessionContextSearchRequest] = {}
        self.search_errors: dict[str, Exception] = {}
        self.search_results: dict[str, SessionContextSearchResultDTO] = {}

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        self.inventory_calls += 1
        return GatewayWorkspaceListDTO(
            active_workspace_id=self.workspace_ids[0] if self.workspace_ids else None,
            items=[
                self._workspace_item(workspace_id)
                for workspace_id in self.workspace_ids
            ],
        )

    def _workspace_item(self, workspace_id: str) -> GatewayWorkspaceDTO:
        workspace = _workspace(
            workspace_id,
            checked_at=f"2026-07-22T00:00:{self.inventory_calls:02d}Z",
            name=self.workspace_names.get(workspace_id),
        )
        if workspace_id in self.missing_backend_workspace_ids:
            workspace = workspace.model_copy(update={"backend_workspace_id": None})
        elif workspace_id in self.backend_workspace_ids:
            workspace = workspace.model_copy(
                update={
                    "backend_workspace_id": self.backend_workspace_ids.get(workspace_id)
                }
            )
        if workspace_id in self.remote_workspaces:
            workspace = workspace.model_copy(update={"connection_kind": "remote_gateway", "remote": self.remote_workspaces[workspace_id]})
        return workspace

    async def read_resource_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return SessionContextReadResultDTO(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            view=request.view,
            revision="rev-read",
        )

    async def search_resource_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        resource = parse_session_context_ref(
            resource_identity=request.resource_identity, uri=request.uri, scope=request.scope,
        )
        workspace_id = next(
            item for item in self.workspace_ids
            if self._workspace_item(item).backend_workspace_id == resource.scope_id
        )
        self.visited.append(workspace_id)
        self.search_requests[workspace_id] = request
        error = self.search_errors.get(workspace_id)
        if error is not None:
            raise error
        return self.search_results.get(workspace_id) or _workspace_search_result(
            workspace_id,
            query=request.query,
        )


@pytest.mark.asyncio
async def test_gateway_inventory_cursor_ignores_dynamic_checked_at() -> None:
    transport = _FakeTransport([_workspace_id(1), _workspace_id(2)])
    service = GatewayContextQueryService(transport=transport)

    first = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1)
    )
    assert first.next_cursor is not None

    second = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor)
    )

    item = second.items[0]
    assert item.scope == "workspace"
    assert item.uri == workspace_session_inventory_uri(_backend_workspace_id(2))
    assert item.resource_identity == workspace_session_inventory_identity(
        _backend_workspace_id(2)
    )
    assert second.revision == first.revision


@pytest.fixture
def root_free_inventory_transport() -> _FakeTransport:
    return _FakeTransport([_workspace_id(1), _workspace_id(2)])


@pytest.mark.asyncio
async def test_gateway_inventory_without_root_tracks_public_name_revision(
    root_free_inventory_transport: _FakeTransport,
) -> None:
    transport = root_free_inventory_transport
    service = GatewayContextQueryService(transport=transport)
    inventory = await service.list_gateway_workspaces()
    assert all("root_path" not in item.model_dump() for item in inventory.items)
    first = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    repeated = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert first.revision == repeated.revision
    assert first.items
    assert all("root_path" not in item.data for item in first.items)

    transport.workspace_names[_workspace_id(2)] = "Renamed workspace"
    changed = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert changed.revision != first.revision
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_gateway_context(_gateway_read_request(view="inventory", revision=first.revision))


@pytest.mark.asyncio
async def test_gateway_inventory_cursor_is_bound_to_backend_workspace_identity() -> (
    None
):
    workspace_ids = [_workspace_id(1), _workspace_id(2)]
    transport = _FakeTransport(workspace_ids)
    service = GatewayContextQueryService(transport=transport)
    first = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1)
    )
    assert first.next_cursor is not None

    transport.backend_workspace_ids[workspace_ids[1]] = str(UUID(int=100))

    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor)
        )


@pytest.mark.asyncio
async def test_gateway_inventory_fails_closed_when_backend_identity_is_missing() -> (
    None
):
    workspace_id = _workspace_id(1)
    transport = _FakeTransport([workspace_id])
    transport.missing_backend_workspace_ids.add(workspace_id)
    service = GatewayContextQueryService(transport=transport)

    with pytest.raises(
        WorkspaceSessionContextAccessError,
        match="缺少真实 backend_workspace_id",
    ):
        await service.read_gateway_context(_gateway_read_request(view="inventory"))


@pytest.mark.asyncio
async def test_gateway_inventory_uses_workspace_unsupported_view_contract() -> None:
    transport = _FakeTransport([_workspace_id(1)])
    service = GatewayContextQueryService(transport=transport)

    with pytest.raises(ResourceUriResolveError) as error:
        await service.read_gateway_context(_gateway_read_request(view="overview"))

    assert error.value.reason_code == "unsupported_view"
    assert transport.inventory_calls == 0


@pytest.mark.parametrize(
    "selector",
    [
        {"record_index": 0},
        {"assembly_ref": session_resource_identity("backend", "assembly")},
    ],
)
@pytest.mark.asyncio
async def test_gateway_inventory_rejects_incompatible_structured_selectors(
    selector: dict[str, object],
) -> None:
    transport = _FakeTransport([_workspace_id(1)])
    service = GatewayContextQueryService(transport=transport)

    with pytest.raises(ValueError):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", **selector)
        )

    assert transport.inventory_calls == 0


@pytest.mark.asyncio
async def test_gateway_inventory_never_returns_a_non_advancing_cursor() -> None:
    workspace_id = _workspace_id(1)
    transport = _FakeTransport([workspace_id])
    # The inventory payload is deliberately large while its real IDs remain distinct.
    transport.workspace_names[workspace_id] = "x" * 2_000
    service = GatewayContextQueryService(transport=transport)

    with pytest.raises(ValueError, match="首个 item"):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", max_chars=256)
        )


@pytest.mark.asyncio
async def test_empty_gateway_inventory_rejects_oversized_envelope() -> None:
    service = GatewayContextQueryService(transport=_FakeTransport([]))

    with pytest.raises(ValueError, match="基础响应 envelope"):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", max_chars=256)
        )


@pytest.mark.asyncio
async def test_gateway_inventory_rolls_back_item_when_final_cursor_exceeds_budget() -> (
    None
):
    transport = _FakeTransport([_workspace_id(i) for i in range(1, 4)])
    service = GatewayContextQueryService(transport=transport)

    result = await service.read_gateway_context(
        _gateway_read_request(view="inventory", max_chars=2_000)
    )

    assert result.items
    assert result.has_more is True
    assert result.next_cursor is not None
    assert result.returned_chars == len(result.model_dump_json())
    assert result.returned_chars <= 2_000


@pytest.mark.asyncio
async def test_gateway_search_routes_by_control_id_but_uses_backend_uuid_for_uri() -> (
    None
):
    control_workspace_id = _workspace_id(1)
    backend_workspace_id = _backend_workspace_id(1)
    transport = _FakeTransport([control_workspace_id])
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        _gateway_search_request(query="marker")
    )

    workspace_request = transport.search_requests[control_workspace_id]
    assert workspace_request.scope == "workspace"
    assert workspace_request.uri == workspace_session_inventory_uri(
        backend_workspace_id
    )
    assert workspace_request.resource_identity == workspace_session_inventory_identity(
        backend_workspace_id
    )
    assert control_workspace_id not in workspace_request.uri
    assert result.partial_errors == []


@pytest.mark.asyncio
async def test_gateway_search_partial_errors_obey_total_character_budget() -> None:
    workspace_ids = [_workspace_id(index) for index in range(25)]
    transport = _FakeTransport(workspace_ids)
    transport.search_errors = {
        workspace_id: RuntimeError("远端失败" * 1_000) for workspace_id in workspace_ids
    }
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        _gateway_search_request(query="marker", max_chars=1_200)
    )

    assert result.returned_chars <= 1_200
    assert result.returned_chars == len(result.model_dump_json())
    assert result.truncated is True
    assert 0 < len(result.partial_errors) < len(workspace_ids)
    assert result.omitted_partial_error_count == (
        len(workspace_ids) - len(result.partial_errors)
    )


@pytest.mark.asyncio
async def test_gateway_search_propagates_workspace_truncation() -> None:
    workspace_id = _workspace_id(1)
    backend_workspace_id = _backend_workspace_id(1)
    transport = _FakeTransport([workspace_id])
    transport.search_results[workspace_id] = _workspace_search_result(
        workspace_id,
        query="marker",
        total_matches=201,
        matches=[_session_match(backend_workspace_id, "ses_1")],
        has_more=True,
        truncated=True,
        next_cursor="workspace-cursor",
    )
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        _gateway_search_request(query="marker")
    )

    assert result.has_more is False
    assert result.truncated is True
    assert result.total_matches == 201
    assert "fan-out" in result.partial_errors[0].error


@pytest.mark.asyncio
async def test_gateway_search_rolls_back_match_when_final_cursor_exceeds_budget() -> (
    None
):
    workspace_id = _workspace_id(1)
    backend_workspace_id = _backend_workspace_id(1)
    transport = _FakeTransport([workspace_id])
    transport.search_results[workspace_id] = _workspace_search_result(
        workspace_id,
        query="marker",
        total_matches=2,
        matches=[
            _session_match(
                backend_workspace_id,
                f"ses_{index}",
                preview="marker " + "x" * 500,
            )
            for index in range(2)
        ],
    )
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        _gateway_search_request(query="marker", max_chars=2_000)
    )

    assert result.matches
    assert result.has_more is True
    assert result.next_cursor is not None
    assert result.returned_chars == len(result.model_dump_json())
    assert result.returned_chars <= 2_000


@pytest.mark.asyncio
async def test_gateway_search_rejects_a_match_from_another_workspace() -> None:
    workspace_id = _workspace_id(1)
    foreign_backend_workspace_id = _backend_workspace_id(2)
    transport = _FakeTransport([workspace_id])
    transport.search_results[workspace_id] = _workspace_search_result(
        workspace_id,
        query="marker",
        total_matches=1,
        matches=[_session_match(foreign_backend_workspace_id, "ses_foreign")],
    )
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        _gateway_search_request(query="marker")
    )

    assert result.matches == []
    assert len(result.partial_errors) == 1
    assert "其他 Workspace" in result.partial_errors[0].error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "max_chars"),
    [("marker", 256), ("marker" * 400, 1_024)],
)
async def test_gateway_search_rejects_oversized_base_envelope(
    query: str,
    max_chars: int,
) -> None:
    service = GatewayContextQueryService(transport=_FakeTransport([]))

    with pytest.raises(ValueError, match="基础响应 envelope"):
        await service.search_gateway_context(
            _gateway_search_request(query=query, max_chars=max_chars)
        )


async def test_gateway_inventory_and_fanout_preserve_remote_gateway_authority() -> None:
    route = _workspace_id(2)
    backend_id = _backend_workspace_id(2)
    transport = _FakeTransport([_workspace_id(1), route])
    transport.remote_workspaces[route] = GatewayRemoteConnectionSummaryDTO(
        gateway_connection_id="connection-remote", remote_workspace_id="remote-route",
        gateway_id="gateway_remote", name="remote", host="remote.example", port=22,
        username="test", remote_gateway_port=8014,
    )
    remote_uri = workspace_session_inventory_uri(backend_id, gateway_authority="gateway_remote")
    remote_match = _session_match(backend_id, "remote-session").model_copy(update={
        "uri": session_resource_uri(backend_id, "remote-session", gateway_authority="gateway_remote"),
    })
    transport.search_results[route] = _workspace_search_result(
        route, query="marker", total_matches=1, matches=[remote_match],
    ).model_copy(update={"uri": remote_uri})
    service = GatewayContextQueryService(transport=transport)

    inventory = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert inventory.items[0].uri == workspace_session_inventory_uri(_backend_workspace_id(1))
    assert inventory.items[1].uri == remote_uri
    result = await service.search_gateway_context(_gateway_search_request(query="marker"))
    assert result.partial_errors == []
    assert result.matches == [remote_match]
    assert transport.search_requests[route].uri == remote_uri
    assert transport.search_requests[route].resource_identity == workspace_session_inventory_identity(backend_id)
