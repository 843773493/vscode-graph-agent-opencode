from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.api.sessions import (
    fork_session_context,
    get_fork_source_point,
    transfer_session_fork,
)
from app.core.resource_uri import thread_resource_uri
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionForkCreationKind,
    SessionForkMode,
    SessionForkRequest,
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_uri,
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
            target_gateway_id="gateway_test",
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
            main_container_uri=workspace_session_inventory_uri(WORKSPACE_ID),
            title="完整历史分支", goal_available=False, resources=[],
            resource_mode="full_rollout_copy", resource_anchor_mode="inclusive",
        )


class _FailingForkService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fork(self, request: SessionForkRequest) -> SessionForkResultDTO:
        raise self.error

    async def transfer(self, request: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        raise self.error


@pytest.fixture
def fork_service() -> _ForkService:
    return _ForkService()


@pytest.fixture
def failing_fork_service(request: pytest.FixtureRequest) -> _FailingForkService:
    return _FailingForkService(request.param)


def _request(mode: SessionForkMode, creation_kind: SessionForkCreationKind, relationship: Literal["detached", "pinned"]) -> SessionForkRequest:
    return SessionForkRequest(
        idempotency_key="api-fork", source_uri=thread_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID),
        source_view_id="view-source", source_view_revision=4,
        anchor_item_id="item-clicked" if mode == "view_prefix_fork" else None,
        mode=mode, creation_kind=creation_kind, relationship=relationship,
        target_container_uri=(session_resource_uri(WORKSPACE_ID, SOURCE_SESSION_ID)
                              if creation_kind == "new_child_thread" else workspace_session_inventory_uri(WORKSPACE_ID)),
    )


@pytest.mark.parametrize(
    ("mode", "creation_kind", "relationship"),
    [
        ("view_prefix_fork", "new_child_thread", "detached"),
        ("last_completed_turn_fork", "new_child_thread", "pinned"),
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


def test_old_fork_scope_is_rejected_without_rewriting_frozen_request() -> None:
    payload = _request("last_completed_turn_fork", "new_child_thread", "detached").model_dump(mode="json")
    payload["mode"] = "context_fork"
    frozen = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with pytest.raises(ValidationError, match="mode"):
        SessionForkRequest.model_validate(payload)
    assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == frozen


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


@pytest.mark.parametrize("route", ["fork", "transfer"])
@pytest.mark.parametrize(
    "failing_fork_service",
    [
        WorkspaceSessionContextAccessError("federation-channel-closed: gateway_remote"),
        RuntimeError("fork_response_protocol_invalid: owner/hash 不匹配"),
    ],
    indirect=True,
)
async def test_fork_routes_classify_access_failure_without_hiding_integrity_error(
    failing_fork_service: _FailingForkService, route: str,
) -> None:
    payload = _request("full_rollout_copy", "new_session_main", "detached")
    arguments = {"_": "local", "request_id": "req_fork_access_failure", "fork_service": failing_fork_service}
    if route == "fork":
        call = fork_session_context(SOURCE_SESSION_ID, payload=payload, **arguments)
    else:
        transfer_request = SessionForkTransferRequest(
            source_gateway_id="gateway_test", action="create", request=payload,
            fork_target=ForkGrantTarget(
                gateway_id="gateway_test", uri=payload.target_container_uri, fork_role="target",
            ),
        )
        call = transfer_session_fork(payload=transfer_request, **arguments)
    if isinstance(failing_fork_service.error, WorkspaceSessionContextAccessError):
        with pytest.raises(HTTPException) as raised:
            await call
        assert raised.value.status_code == 502
        assert raised.value.detail == str(failing_fork_service.error)
        assert raised.value.__cause__ is failing_fork_service.error
    else:
        with pytest.raises(RuntimeError) as raised:
            await call
        assert raised.value is failing_fork_service.error
