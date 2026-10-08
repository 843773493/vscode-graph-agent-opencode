from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.gateway.workspaces import GatewayWorkspaceDTO, GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import (
    SessionContextSearchMatchDTO,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
)
from app.services.business.session_target_resolver import SessionTargetResolver


def _gateway_workspace(
    workspace_id: str,
    backend_workspace_id: str,
) -> GatewayWorkspaceDTO:
    return GatewayWorkspaceDTO(
        workspace_id=workspace_id,
        backend_workspace_id=backend_workspace_id,
        name=workspace_id,
        root_path="/workspace",
        backend_url="http://127.0.0.1:8010",
        connection_kind="local",
        status="ready",
        runtime_action="probe_external_backend",
        checked_at="2026-10-04T00:00:00Z",
    )


class _FakeSessionService:
    def __init__(self, local_sessions: dict[str, str]) -> None:
        self.local_sessions = local_sessions

    async def get(self, session_id: str):
        if session_id not in self.local_sessions:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"Session {session_id} not found")
        return SimpleNamespace(
            session_id=session_id,
            workspace_id=self.local_sessions[session_id],
        )


class _FakeGatewayContextClient:
    def __init__(
        self,
        *,
        backend_sessions: list[tuple[str, str]],
        workspaces: list[tuple[str, str]],
        truncated: bool = False,
        partial_errors: list[tuple[str, str]] | None = None,
        omitted_partial_error_count: int = 0,
    ) -> None:
        self.backend_sessions = backend_sessions
        self.truncated = truncated
        self.partial_errors = partial_errors or []
        self.omitted_partial_error_count = omitted_partial_error_count
        self.inventory = GatewayWorkspaceListDTO(
            items=[_gateway_workspace(workspace_id, backend_id)
                   for workspace_id, backend_id in workspaces]
        )
        self.search_requests = []

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        return self.inventory

    async def search_gateway_context(self, request):
        self.search_requests.append(request)
        return SessionContextSearchResultDTO(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            query=request.query,
            match_mode=request.match_mode,
            revision="gateway-revision",
            truncated=self.truncated,
            partial_errors=[
                {
                    "resource_identity": session_resource_identity(backend_id, "ses_partial"),
                    "error": message,
                }
                for backend_id, message in self.partial_errors
            ],
            omitted_partial_error_count=self.omitted_partial_error_count,
            matches=[
                SessionContextSearchMatchDTO(
                    resource_identity=session_resource_identity(backend_id, session_id),
                    uri=session_resource_uri(backend_id, session_id),
                    scope="workspace",
                    preview="session",
                    source="session_catalog",
                    revision="workspace-revision",
                    match_start=0,
                    match_end=len(request.query),
                )
                for backend_id, session_id in self.backend_sessions
            ],
        )


@pytest.mark.asyncio
async def test_resolver_prefers_local_session_without_gateway_lookup(monkeypatch):
    gateway = _FakeGatewayContextClient(backend_sessions=[], workspaces=[])
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({"ses_local": "backend-local"}),
        workspace_session_context_client=gateway,
    )

    target = await resolver.resolve_session("ses_local")

    assert target.session_id == "ses_local"
    assert target.workspace_id is None
    assert gateway.search_requests == []


@pytest.mark.asyncio
async def test_resolver_maps_uri_backend_identity_to_gateway_route(monkeypatch):
    monkeypatch.setattr(
        "app.services.business.session_target_resolver.require_current_gateway_id",
        lambda _consumer: "gateway-a",
    )
    gateway = _FakeGatewayContextClient(
        backend_sessions=[("backend-a", "ses_remote")],
        workspaces=[("gw_a", "backend-a")],
    )
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({}),
        workspace_session_context_client=gateway,
    )

    target = await resolver.resolve_session("ses_remote")

    assert target.session_id == "ses_remote"
    assert target.workspace_id == "gw_a"
    request = gateway.search_requests[0]
    assert request.resource_identity == gateway_session_inventory_identity("gateway-a")
    assert request.uri == gateway_session_inventory_uri("gateway-a")
    assert request.scope == "gateway"


@pytest.mark.asyncio
async def test_resolver_requires_gateway_route_when_session_identity_conflicts(monkeypatch):
    monkeypatch.setattr(
        "app.services.business.session_target_resolver.require_current_gateway_id",
        lambda _consumer: "gateway-a",
    )
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({}),
        workspace_session_context_client=_FakeGatewayContextClient(
            backend_sessions=[
                ("backend-a", "ses_duplicate"),
                ("backend-b", "ses_duplicate"),
            ],
            workspaces=[("gw_a", "backend-a"), ("gw_b", "backend-b")],
        ),
    )

    with pytest.raises(WorkspaceSessionContextAccessError, match="补传 gateway_workspace_id"):
        await resolver.resolve_session("ses_duplicate")


@pytest.mark.parametrize(
    "search_state",
    [
        {"partial_errors": [("backend-b", "backend unavailable")]},
        {"truncated": True},
        {"omitted_partial_error_count": 1},
    ],
)
@pytest.mark.asyncio
async def test_resolver_rejects_unique_visible_match_when_gateway_search_is_incomplete(
    monkeypatch,
    search_state: dict[str, object],
) -> None:
    monkeypatch.setattr(
        "app.services.business.session_target_resolver.require_current_gateway_id",
        lambda _consumer: "gateway-a",
    )
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({}),
        workspace_session_context_client=_FakeGatewayContextClient(
            backend_sessions=[("backend-a", "ses_visible")],
            workspaces=[("gw_a", "backend-a")],
            **search_state,
        ),
    )

    with pytest.raises(WorkspaceSessionContextAccessError, match="搜索不完整"):
        await resolver.resolve_session("ses_visible")


@pytest.mark.asyncio
async def test_resolver_requires_inventory_mapping_for_explicit_backend_workspace():
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({}),
        workspace_session_context_client=_FakeGatewayContextClient(
            backend_sessions=[],
            workspaces=[("gw_a", "backend-a")],
        ),
    )

    target = await resolver.resolve_session(
        "ses_remote",
        backend_workspace_id="backend-a",
    )

    assert target == type(target)(session_id="ses_remote", workspace_id="gw_a")


@pytest.mark.asyncio
async def test_resolver_rejects_unknown_backend_workspace_identity():
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({}),
        workspace_session_context_client=_FakeGatewayContextClient(
            backend_sessions=[],
            workspaces=[("gw_a", "backend-a")],
        ),
    )

    with pytest.raises(WorkspaceSessionContextAccessError, match="backend_workspace_id=missing"):
        await resolver.resolve_backend_workspace("missing")
