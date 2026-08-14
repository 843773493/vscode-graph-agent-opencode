from __future__ import annotations

import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.public_v2.session_context import (
    SessionContextSearchMatchDTO,
    SessionContextSearchResultDTO,
)
from app.services.business.session_target_resolver import SessionTargetResolver


class _FakeSessionService:
    def __init__(self, local_session_ids: set[str]) -> None:
        self.local_session_ids = local_session_ids

    async def get(self, session_id: str):
        if session_id not in self.local_session_ids:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"Session {session_id} not found")
        return object()


class _FakeGatewayContextClient:
    def __init__(self, locators: list[str]) -> None:
        self.locators = locators

    async def search_gateway_context(self, request):
        return SessionContextSearchResultDTO(
            resource=request.resource,
            query=request.query,
            match_mode=request.match_mode,
            revision="gateway-revision",
            matches=[
                SessionContextSearchMatchDTO(
                    locator=locator,
                    preview="session",
                    source="session_catalog",
                    revision="workspace-revision",
                    match_start=0,
                    match_end=len(request.query),
                )
                for locator in self.locators
            ],
        )


@pytest.mark.asyncio
async def test_resolver_prefers_local_session_without_gateway_lookup():
    gateway = _FakeGatewayContextClient([])
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService({"ses_local"}),
        workspace_session_context_client=gateway,
    )

    target = await resolver.resolve_session("ses_local")

    assert target.session_id == "ses_local"
    assert target.workspace_id is None


@pytest.mark.asyncio
async def test_resolver_finds_remote_session_by_id_without_workspace_id():
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService(set()),
        workspace_session_context_client=_FakeGatewayContextClient(
            ["boxteam://workspace/gw_a/session/ses_remote"]
        ),
    )

    target = await resolver.resolve_session("ses_remote")

    assert target.session_id == "ses_remote"
    assert target.workspace_id == "gw_a"


@pytest.mark.asyncio
async def test_resolver_requires_workspace_id_for_conflicting_session_id():
    resolver = SessionTargetResolver(
        session_service=_FakeSessionService(set()),
        workspace_session_context_client=_FakeGatewayContextClient(
            [
                "boxteam://workspace/gw_a/session/ses_duplicate",
                "boxteam://workspace/gw_b/session/ses_duplicate",
            ]
        ),
    )

    with pytest.raises(WorkspaceSessionContextAccessError, match="补传 workspace_id"):
        await resolver.resolve_session("ses_duplicate")
