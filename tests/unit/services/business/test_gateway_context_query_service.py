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
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import (
    GatewayContextSearchRequest,
    GatewayContextSearchResultDTO,
)
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
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








class _FakeTransport:
    def __init__(self, workspace_ids: list[str]) -> None:
        self.workspace_ids = workspace_ids
        self.workspace_names: dict[str, str] = {}
        self.remote_workspaces: dict[str, GatewayRemoteConnectionSummaryDTO] = {}
        self.backend_workspace_ids: dict[str, str] = {}
        self.missing_backend_workspace_ids: set[str] = set()
        self.inventory_calls = 0
        self.visited: list[str] = []

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

    async def search_resource_context(self, request: SessionContextSearchRequest) -> SessionContextSearchResultDTO:
        return SessionContextSearchResultDTO(
            **request.model_dump(include={"resource_identity", "uri", "scope", "query", "match_mode"}), revision="local",
        )

    async def search_gateway_context(self, request: GatewayContextSearchRequest) -> GatewayContextSearchResultDTO:
        raise AssertionError("inventory 读取不得触发正文搜索")


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




















async def test_gateway_inventory_preserves_remote_gateway_authority() -> None:
    route = _workspace_id(2)
    backend_id = _backend_workspace_id(2)
    transport = _FakeTransport([_workspace_id(1), route])
    transport.remote_workspaces[route] = GatewayRemoteConnectionSummaryDTO(
        gateway_connection_id="connection-remote", remote_workspace_id="remote-route",
        gateway_id="gateway_remote", name="remote", host="remote.example", port=22,
        username="test", remote_gateway_port=8014,
    )
    service = GatewayContextQueryService(transport=transport)

    inventory = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert inventory.items[0].uri == workspace_session_inventory_uri(_backend_workspace_id(1))
    assert inventory.items[1].uri == workspace_session_inventory_uri(backend_id, gateway_authority="gateway_remote")
