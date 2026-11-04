from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.gateway.workspaces import (
    GatewayConnectionKind,
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
REMOTE_GATEWAY_ID = "gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER_REMOTE_GATEWAY_ID = "gateway_cccccccccccccccccccccccccccccccc"


@pytest.fixture(autouse=True)
def _current_gateway_identity() -> Generator[None, None, None]:
    token = set_current_gateway_id(GATEWAY_ID)
    yield
    reset_current_gateway_id(token)


def _workspace_id(index: int) -> str:
    return str(UUID(int=index + 1))


def _workspace(
    workspace_id: str,
    *,
    checked_at: str,
    gateway_id: str = GATEWAY_ID,
    connection_kind: GatewayConnectionKind = "local",
    remote_gateways: list[GatewayRemoteConnectionSummaryDTO] | None = None,
) -> GatewayWorkspaceDTO:
    return GatewayWorkspaceDTO(
        gateway_id=gateway_id,
        workspace_id=workspace_id,
        name=workspace_id,
        backend_url=f"http://127.0.0.1/{workspace_id}",
        connection_kind=connection_kind,
        status="ready",
        runtime_action="probe_external_backend",
        remote_gateways=remote_gateways or [],
        checked_at=checked_at,
    )


def _gateway_read_request(**kwargs: object) -> SessionContextReadRequest:
    return SessionContextReadRequest.model_validate({
        "resource_identity": gateway_session_inventory_identity(GATEWAY_ID),
        "uri": gateway_session_inventory_uri(GATEWAY_ID),
        "scope": "gateway",
        **kwargs,
    })


class _FakeTransport:
    def __init__(self, workspace_ids: list[str]) -> None:
        self.workspaces = [
            _workspace(workspace_id, checked_at="2026-07-22T00:00:00Z")
            for workspace_id in workspace_ids
        ]
        self.active_workspace = (
            GatewaySearchWorkspaceOwnerDTO(gateway_id=GATEWAY_ID, workspace_id=workspace_ids[0])
            if workspace_ids else None
        )
        self.inventory_calls = 0

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        self.inventory_calls += 1
        return GatewayWorkspaceListDTO(
            active_workspace=self.active_workspace,
            items=[
                GatewayWorkspaceDTO.model_validate({
                    **workspace.model_dump(mode="json"),
                    "checked_at": f"2026-07-22T00:00:{self.inventory_calls:02d}Z",
                })
                for workspace in self.workspaces
            ],
        )

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
    assert first.items[0].uri == workspace_session_inventory_uri(_workspace_id(1))
    assert transport.inventory_calls == 1

    second = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor)
    )

    item = second.items[0]
    assert item.scope == "workspace"
    assert item.uri == workspace_session_inventory_uri(_workspace_id(2))
    assert item.resource_identity == workspace_session_inventory_identity(
        _workspace_id(2)
    )
    assert second.revision == first.revision
    assert transport.inventory_calls == 2


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
    assert inventory.active_workspace == GatewaySearchWorkspaceOwnerDTO(
        gateway_id=GATEWAY_ID, workspace_id=_workspace_id(1),
    )
    assert all("root_path" not in item.model_dump() for item in inventory.items)
    first = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    repeated = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert first.revision == repeated.revision
    assert first.items
    assert all(item.data is not None and "root_path" not in item.data for item in first.items)

    transport.workspaces[1].name = "Renamed workspace"
    changed = await service.read_gateway_context(_gateway_read_request(view="inventory"))
    assert changed.revision != first.revision
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_gateway_context(_gateway_read_request(view="inventory", revision=first.revision))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_owner_field", ["workspace_id", "gateway_id"])
async def test_gateway_inventory_cursor_is_bound_to_workspace_owner(changed_owner_field: str) -> None:
    workspace_ids = [_workspace_id(1), _workspace_id(2)]
    transport = _FakeTransport(workspace_ids)
    transport.workspaces[1] = _workspace(
        workspace_ids[1], checked_at="2026-07-22T00:00:00Z",
        gateway_id=REMOTE_GATEWAY_ID, connection_kind="remote_gateway",
    )
    service = GatewayContextQueryService(transport=transport)
    first = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1)
    )
    assert first.next_cursor is not None

    replacement = transport.workspaces[1].model_dump(mode="json")
    replacement[changed_owner_field] = (
        _workspace_id(100) if changed_owner_field == "workspace_id" else OTHER_REMOTE_GATEWAY_ID
    )
    transport.workspaces[1] = GatewayWorkspaceDTO.model_validate(replacement)

    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor)
        )


@pytest.mark.parametrize("field", ["gateway_id", "workspace_id"])
@pytest.mark.parametrize("missing", [True, False], ids=["missing", "null"])
def test_gateway_inventory_rejects_missing_owner_at_public_dto_boundary(field: str, missing: bool) -> None:
    payload = _workspace(_workspace_id(1), checked_at="2026-07-22T00:00:00Z").model_dump(mode="json")
    if missing:
        payload.pop(field)
    else:
        payload[field] = None
    with pytest.raises(ValidationError) as error:
        GatewayWorkspaceDTO.model_validate(payload)
    assert [(entry["loc"], entry["type"]) for entry in error.value.errors()] == [
        ((field,), "missing" if missing else "string_type"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("gateway_id", "gateway_remote"), ("workspace_id", "gateway_workspace_1")],
)
def test_gateway_inventory_rejects_noncanonical_active_owner(field: str, value: str) -> None:
    owner = {"gateway_id": GATEWAY_ID, "workspace_id": _workspace_id(1), field: value}
    with pytest.raises(ValidationError) as error:
        GatewayWorkspaceListDTO.model_validate({"active_workspace": owner, "items": []})
    assert [entry["loc"] for entry in error.value.errors()] == [("active_workspace", field)]


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
        {"assembly_ref": session_resource_identity(_workspace_id(1), "assembly")},
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
    # 故意放大公开名称，保持真实工作区身份不变。
    transport.workspaces[0].name = "x" * 2_000
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
async def test_gateway_inventory_rolls_back_item_when_final_cursor_exceeds_budget() -> None:
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
@pytest.mark.parametrize("remote_workspace_index", [1, 2], ids=["same-uuid", "different-uuid"])
async def test_gateway_inventory_preserves_remote_gateway_authority(remote_workspace_index: int) -> None:
    local_id = _workspace_id(1)
    remote_id = _workspace_id(remote_workspace_index)
    transport = _FakeTransport([local_id, remote_id])
    remote_gateway = GatewayRemoteConnectionSummaryDTO(
        gateway_connection_id="connection-remote",
        gateway_id=REMOTE_GATEWAY_ID, name="remote", host="remote.example", port=22,
        username="test", remote_gateway_port=8014,
    )
    transport.workspaces[1] = _workspace(
        remote_id, checked_at="2026-07-22T00:00:00Z", gateway_id=REMOTE_GATEWAY_ID,
        connection_kind="remote_gateway", remote_gateways=[remote_gateway],
    )
    service = GatewayContextQueryService(transport=transport)

    inventory = await service.list_gateway_workspaces()
    assert inventory.items[1].remote_gateways == [remote_gateway]
    first = await service.read_gateway_context(_gateway_read_request(view="inventory", limit=1))
    assert first.next_cursor is not None
    second = await service.read_gateway_context(
        _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor),
    )
    local_item, = first.items
    remote_item, = second.items
    assert local_item.uri == workspace_session_inventory_uri(local_id)
    assert remote_item.uri == workspace_session_inventory_uri(remote_id, gateway_authority=REMOTE_GATEWAY_ID)
    assert local_item.resource_identity == workspace_session_inventory_identity(local_id)
    assert remote_item.resource_identity == workspace_session_inventory_identity(remote_id)
    assert local_item.data is not None and remote_item.data is not None
    assert (local_item.data["gateway_id"], local_item.data["workspace_id"]) == (GATEWAY_ID, local_id)
    assert (remote_item.data["gateway_id"], remote_item.data["workspace_id"]) == (REMOTE_GATEWAY_ID, remote_id)
    assert second.revision == first.revision and second.has_more is False and second.next_cursor is None
    transport.active_workspace = GatewaySearchWorkspaceOwnerDTO(
        gateway_id=REMOTE_GATEWAY_ID, workspace_id=remote_id,
    )
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_gateway_context(
            _gateway_read_request(view="inventory", limit=1, cursor=first.next_cursor),
        )
