from __future__ import annotations

import pytest

from app.schemas.gateway import GatewayWorkspaceDTO, GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import (
    SessionContextSearchMatchDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)


def _workspace(workspace_id: str, *, checked_at: str) -> GatewayWorkspaceDTO:
    return GatewayWorkspaceDTO(
        workspace_id=workspace_id,
        name=workspace_id,
        root_path=f"/workspaces/{workspace_id}",
        backend_url=f"http://127.0.0.1/{workspace_id}",
        connection_kind="local",
        status="ready",
        runtime_action="probe_external_backend",
        checked_at=checked_at,
    )


class _FakeTransport:
    def __init__(self, workspace_ids: list[str]) -> None:
        self.workspace_ids = workspace_ids
        self.inventory_calls = 0
        self.visited: list[str] = []
        self.search_errors: dict[str, Exception] = {}
        self.search_results: dict[str, SessionContextSearchResultDTO] = {}

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        self.inventory_calls += 1
        return GatewayWorkspaceListDTO(
            active_workspace_id=self.workspace_ids[0] if self.workspace_ids else None,
            items=[
                _workspace(
                    workspace_id,
                    checked_at=f"2026-07-22T00:00:{self.inventory_calls:02d}Z",
                )
                for workspace_id in self.workspace_ids
            ],
        )

    async def read_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return SessionContextReadResultDTO(
            resource=request.resource,
            view=request.view,
            revision=f"rev-{workspace_id}",
        )

    async def search_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        self.visited.append(workspace_id)
        error = self.search_errors.get(workspace_id)
        if error is not None:
            raise error
        result = self.search_results.get(workspace_id)
        if result is not None:
            return result
        return SessionContextSearchResultDTO(
            resource=request.resource,
            query=request.query,
            match_mode=request.match_mode,
            revision=f"rev-{workspace_id}",
        )


@pytest.mark.asyncio
async def test_gateway_inventory_cursor_ignores_dynamic_checked_at() -> None:
    transport = _FakeTransport(["ws_1", "ws_2"])
    service = GatewayContextQueryService(transport=transport)

    first = await service.read_gateway_context(
        SessionContextReadRequest(
            resource="boxteam://gateway/workspaces",
            view="inventory",
            limit=1,
        )
    )
    assert first.next_cursor is not None

    second = await service.read_gateway_context(
        SessionContextReadRequest(
            resource="boxteam://gateway/workspaces",
            view="inventory",
            limit=1,
            cursor=first.next_cursor,
        )
    )

    assert second.items[0].locator == "boxteam://workspace/ws_2"
    assert second.revision == first.revision


@pytest.mark.asyncio
async def test_gateway_inventory_never_returns_a_non_advancing_cursor() -> None:
    transport = _FakeTransport(["ws_" + "x" * 2_000])
    service = GatewayContextQueryService(transport=transport)

    with pytest.raises(ValueError, match="首个 item"):
        await service.read_gateway_context(
            SessionContextReadRequest(
                resource="boxteam://gateway/workspaces",
                view="inventory",
                max_chars=256,
            )
        )


@pytest.mark.asyncio
async def test_empty_gateway_inventory_rejects_oversized_envelope() -> None:
    service = GatewayContextQueryService(transport=_FakeTransport([]))

    with pytest.raises(ValueError, match="基础响应 envelope"):
        await service.read_gateway_context(
            SessionContextReadRequest(
                resource="boxteam://gateway/workspaces",
                view="inventory",
                max_chars=256,
            )
        )


@pytest.mark.asyncio
async def test_gateway_inventory_rolls_back_item_when_final_cursor_exceeds_budget() -> None:
    transport = _FakeTransport(["ws_1", "ws_2", "ws_3"])
    service = GatewayContextQueryService(transport=transport)

    result = await service.read_gateway_context(
        SessionContextReadRequest(
            resource="boxteam://gateway/workspaces",
            view="inventory",
            max_chars=2_000,
        )
    )

    assert result.items
    assert result.has_more is True
    assert result.next_cursor is not None
    assert result.returned_chars == len(result.model_dump_json())
    assert result.returned_chars <= 2_000


@pytest.mark.asyncio
async def test_gateway_search_visits_every_workspace() -> None:
    workspace_ids = [f"ws_{index}" for index in range(25)]
    transport = _FakeTransport(workspace_ids)
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        SessionContextSearchRequest(
            resource="boxteam://gateway",
            query="marker",
        )
    )

    assert sorted(transport.visited) == sorted(workspace_ids)
    assert result.partial_errors == []


@pytest.mark.asyncio
async def test_gateway_search_partial_errors_obey_total_character_budget() -> None:
    workspace_ids = [f"ws_{index}" for index in range(25)]
    transport = _FakeTransport(workspace_ids)
    transport.search_errors = {
        workspace_id: RuntimeError("远端失败" * 1_000)
        for workspace_id in workspace_ids
    }
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        SessionContextSearchRequest(
            resource="boxteam://gateway",
            query="marker",
            max_chars=1_024,
        )
    )

    assert result.returned_chars <= 1_024
    assert result.returned_chars == len(result.model_dump_json())
    assert result.truncated is True
    assert 0 < len(result.partial_errors) < len(workspace_ids)
    assert result.omitted_partial_error_count == (
        len(workspace_ids) - len(result.partial_errors)
    )


@pytest.mark.asyncio
async def test_gateway_search_propagates_workspace_truncation() -> None:
    transport = _FakeTransport(["ws_1"])
    transport.search_results["ws_1"] = SessionContextSearchResultDTO(
        resource="boxteam://workspace/ws_1/sessions",
        query="marker",
        match_mode="literal",
        revision="rev-ws_1",
        total_matches=201,
        matches=[
            SessionContextSearchMatchDTO(
                locator="boxteam://workspace/ws_1/session/ses_1#record=0",
                preview="marker",
                source="effective_context",
                revision="rev-session",
                record_index=0,
                match_start=0,
                match_end=6,
            )
        ],
        has_more=True,
        truncated=True,
        next_cursor="workspace-cursor",
    )
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        SessionContextSearchRequest(
            resource="boxteam://gateway",
            query="marker",
        )
    )

    assert result.has_more is False
    assert result.truncated is True
    assert result.total_matches == 201
    assert "fan-out" in result.partial_errors[0].error


@pytest.mark.asyncio
async def test_gateway_search_rolls_back_match_when_final_cursor_exceeds_budget() -> None:
    transport = _FakeTransport(["ws_1"])
    transport.search_results["ws_1"] = SessionContextSearchResultDTO(
        resource="boxteam://workspace/ws_1/sessions",
        query="marker",
        match_mode="literal",
        revision="rev-ws_1",
        total_matches=2,
        matches=[
            SessionContextSearchMatchDTO(
                locator=f"boxteam://workspace/ws_1/session/ses_{index}#record=0",
                preview="marker " + "x" * 500,
                source="effective_context",
                revision=f"rev-session-{index}",
                record_index=0,
                match_start=0,
                match_end=6,
            )
            for index in range(2)
        ],
    )
    service = GatewayContextQueryService(transport=transport)

    result = await service.search_gateway_context(
        SessionContextSearchRequest(
            resource="boxteam://gateway",
            query="marker",
            max_chars=1_400,
        )
    )

    assert result.matches
    assert result.has_more is True
    assert result.next_cursor is not None
    assert result.returned_chars == len(result.model_dump_json())
    assert result.returned_chars <= 1_400


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
            SessionContextSearchRequest(
                resource="boxteam://gateway",
                query=query,
                max_chars=max_chars,
            )
        )
