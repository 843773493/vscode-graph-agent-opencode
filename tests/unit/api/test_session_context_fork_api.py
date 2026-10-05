from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.api.sessions import fork_session_context
from app.schemas.internal_v2.session import SessionDTO, SessionForkRequest

SOURCE_SESSION_ID = "ses_source"


class _ForkService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def fork(
        self,
        source_session_id: str,
        **kwargs: object,
    ) -> SessionDTO:
        self.calls.append((source_session_id, kwargs))
        now = datetime.now(UTC)
        place_under_source = kwargs["place_under_source"] is True
        return SessionDTO(
            session_id="ses_child",
            workspace_id="00000000-0000-4000-8000-000000000001",
            title="来源会话副本",
            current_agent_id="agent_default",
            parent_session_id=source_session_id if place_under_source else None,
            context_source_session_id=source_session_id,
            kind="context_fork",
            created_at=now,
            updated_at=now,
        )


@pytest.mark.parametrize(
    ("payload", "expected_pinned", "expected_place_under_source"),
    [
        pytest.param(None, False, False, id="request-defaults"),
        pytest.param(
            {"pinned": True, "place_under_source": False},
            True,
            False,
            id="pin-without-placement",
        ),
        pytest.param(
            {"pinned": False, "place_under_source": True},
            False,
            True,
            id="placement-without-pin",
        ),
    ],
)
async def test_fork_route_passes_retention_and_placement_independently(
    payload: dict[str, bool] | None,
    expected_pinned: bool,
    expected_place_under_source: bool,
) -> None:
    service = _ForkService()

    response = await fork_session_context(
        SOURCE_SESSION_ID,
        payload=(
            SessionForkRequest.model_validate(payload) if payload is not None else None
        ),
        _="local",
        request_id="req_session_fork",
        fork_service=service,
    )

    assert response.request_id == "req_session_fork"
    assert response.data is not None
    assert response.data.parent_session_id == (
        SOURCE_SESSION_ID if expected_place_under_source else None
    )
    assert len(service.calls) == 1
    called_session_id, called_options = service.calls[0]
    assert called_session_id == SOURCE_SESSION_ID
    assert called_options["pinned"] is expected_pinned
    assert called_options["place_under_source"] is expected_place_under_source
