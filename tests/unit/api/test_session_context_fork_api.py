from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from app.api.sessions import fork_session_context, get_fork_source_point
from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionForkCreationKind,
    SessionForkMode,
    SessionForkRequest,
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)

SOURCE_SESSION_ID = "ses_00000000400070008000000000000001"
SOURCE_THREAD_ID = "thr_00000000400070008000000000000002"
TARGET_SESSION_ID = "ses_00000000400070008000000000000003"
TARGET_THREAD_ID = "thr_00000000400070008000000000000004"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


class _ForkService:
    def __init__(self) -> None:
        self.calls: list[SessionForkRequest] = []
        self.source_calls: list[dict[str, object]] = []

    async def fork(self, request: SessionForkRequest) -> SessionForkResultDTO:
        self.calls.append(request)
        now = datetime.now(UTC)
        session_id = SOURCE_SESSION_ID if request.creation_kind == "new_child_thread" else TARGET_SESSION_ID
        session = SessionDTO(
            session_id=session_id,
            workspace_id=WORKSPACE_ID,
            resource_identity=session_resource_identity(WORKSPACE_ID, session_id),
            uri=session_resource_uri(WORKSPACE_ID, session_id),
            scope="workspace",
            title="来源会话副本",
            current_agent_id="agent_default",
            context_source_session_id=SOURCE_SESSION_ID if request.creation_kind == "new_session_main" else None,
            kind="context_fork" if request.creation_kind == "new_session_main" else "normal",
            created_at=now,
            updated_at=now,
        )
        return SessionForkResultDTO(
            session=session, thread_id=TARGET_THREAD_ID,
            thread_uri=thread_resource_uri(WORKSPACE_ID, session_id, TARGET_THREAD_ID),
            creation_kind=request.creation_kind, source_uri=request.source_uri,
            source_view_id=request.source_view_id, source_view_revision=request.source_view_revision,
            anchor_item_id=request.anchor_item_id, mode=request.mode,
        )

    async def source_point(self, **kwargs: object) -> SessionForkSourcePointDTO:
        self.source_calls.append(kwargs)
        return SessionForkSourcePointDTO(
            source_uri=thread_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID),
            source_view_id="view-source", source_view_revision=4, anchor_item_id=None,
            child_container_uri=session_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID),
            main_container_uri=f"boxteam://workspace/{WORKSPACE_ID}/resources/workspace",
            title="完整历史分支", goal_available=False, resources=[],
            resource_mode="full_rollout_copy", resource_anchor_mode="inclusive",
        )


@pytest.fixture
def fork_service() -> _ForkService:
    return _ForkService()


def _request(mode: SessionForkMode, creation_kind: SessionForkCreationKind, relationship: Literal["detached", "pinned"]) -> SessionForkRequest:
    return SessionForkRequest(
        idempotency_key="api-fork", source_uri=thread_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID),
        source_view_id="view-source", source_view_revision=4,
        anchor_item_id="item-clicked" if mode == "view_prefix_fork" else None,
        mode=mode, creation_kind=creation_kind, relationship=relationship,
        target_container_uri=(session_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID)
                              if creation_kind == "new_child_thread" else f"boxteam://workspace/{WORKSPACE_ID}/resources/workspace"),
    )


@pytest.mark.parametrize(
    ("mode", "creation_kind", "relationship"),
    [
        ("view_prefix_fork", "new_child_thread", "detached"),
        ("context_fork", "new_child_thread", "pinned"),
        ("full_rollout_copy", "new_session_main", "detached"),
    ],
)
async def test_fork_route_preserves_frozen_request_and_complete_result(
    fork_service: _ForkService, mode: SessionForkMode,
    creation_kind: SessionForkCreationKind, relationship: Literal["detached", "pinned"],
) -> None:
    payload = _request(mode, creation_kind, relationship)
    response = await fork_session_context(
        SOURCE_SESSION_ID, payload=payload, _="local",
        request_id="req_session_fork", fork_service=fork_service,
    )
    assert fork_service.calls == [payload] and fork_service.calls[0] is payload
    wire = response.model_dump(mode="json")
    assert wire["request_id"] == "req_session_fork"
    result = wire["data"]
    assert result["session"]["session_id"] == (SOURCE_SESSION_ID if creation_kind == "new_child_thread" else TARGET_SESSION_ID)
    assert result["thread_id"] == TARGET_THREAD_ID
    assert result["thread_uri"] == thread_resource_uri(WORKSPACE_ID, result["session"]["session_id"], TARGET_THREAD_ID)
    for field in ("mode", "creation_kind", "source_uri", "source_view_id", "source_view_revision", "anchor_item_id"):
        assert result[field] == getattr(payload, field)
    assert result["resources"] == [] and result["goal_id"] is None


async def test_fork_route_passes_exact_remote_thread_to_business_boundary(fork_service: _ForkService) -> None:
    source_uri = thread_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gw_remote")
    payload = _request("full_rollout_copy", "new_session_main", "detached").model_copy(update={"source_uri": source_uri})
    await fork_session_context(
        SOURCE_SESSION_ID, payload=payload, _="local",
        request_id="req_session_link_fork", fork_service=fork_service,
    )
    assert fork_service.calls == [payload] and fork_service.calls[0].source_uri == source_uri


async def test_full_source_point_route_serializes_absent_anchor(fork_service: _ForkService) -> None:
    response = await get_fork_source_point(
        SOURCE_SESSION_ID, thread_id=SOURCE_THREAD_ID, turn_id=None, item_id=None,
        source_view_id="view-source", source_view_revision=4, mode="full_rollout_copy", anchor_mode="inclusive",
        _="local", request_id="req_source_point", fork_service=fork_service,
    )
    assert fork_service.source_calls == [{
        "session_id": SOURCE_SESSION_ID, "thread_id": SOURCE_THREAD_ID, "turn_id": None, "item_id": None,
        "source_view_id": "view-source", "source_view_revision": 4, "mode": "full_rollout_copy", "anchor_mode": "inclusive",
    }]
    wire = response.model_dump(mode="json")
    assert wire["request_id"] == "req_source_point"
    assert wire["data"]["anchor_item_id"] is None
    assert wire["data"]["resource_mode"] == "full_rollout_copy"
