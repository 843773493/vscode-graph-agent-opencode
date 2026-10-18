from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.resource_uri import thread_resource_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionForkCreationKind,
    SessionForkRequest,
    SessionForkResultDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_uri,
)
from app.services.business.session_service import SessionService

SOURCE_WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TARGET_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_SESSION_ID = "ses_00000000400070008000000000000001"
SOURCE_THREAD_ID = "thr_00000000400070008000000000000002"
TARGET_SESSION_ID = "ses_00000000400070008000000000000003"
TARGET_THREAD_ID = "thr_00000000400070008000000000000004"
OTHER_SESSION_ID = "ses_00000000400070008000000000000005"
TARGET_CONTAINER_THREAD_ID = "thr_00000000400070008000000000000006"
OTHER_THREAD_ID = "thr_00000000400070008000000000000007"
SOURCE_MAIN_THREAD_ID = "thr_00000000400070008000000000000008"


class _ForkTransport:
    def __init__(self) -> None:
        self.calls: list[SessionForkTransferRequest] = []
        self.reply = SessionForkTransferResultDTO()
        self.error: WorkspaceSessionContextAccessError | None = None

    async def fork_transfer(self, request: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


@dataclass
class _ForkBoundary:
    service: SessionContextForkService
    sessions: Mock
    transport: _ForkTransport
    main_owner: AsyncMock
    child_owner: AsyncMock


@pytest.fixture
def fork_boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ForkBoundary]:
    sessions = Mock(spec=SessionService, workspace_id=SOURCE_WORKSPACE_ID)
    sessions.resolve_main_thread = AsyncMock(side_effect=AssertionError("精确来源 Thread 不得解析 main"))
    transport = _ForkTransport()
    service = SessionContextForkService(
        session_service=sessions, checkpointer=Mock(), thread_creation_factory=Mock(),
        goal_service=Mock(), terminal_resource_owner=Mock(), browser_resource_owner=Mock(),
        transport=transport,
    )
    main_owner = AsyncMock(side_effect=AssertionError("远端目标不得调用本地创建 owner"))
    child_owner = AsyncMock(side_effect=AssertionError("远端目标不得调用本地创建 owner"))
    monkeypatch.setattr(service, "_fork_main", main_owner)
    monkeypatch.setattr(service, "_fork_child", child_owner)
    token = set_current_gateway_id("gateway_origin")
    try:
        yield _ForkBoundary(service, sessions, transport, main_owner, child_owner)
    finally:
        reset_current_gateway_id(token)


@pytest.fixture
def fork_request() -> SessionForkRequest:
    return SessionForkRequest(
        idempotency_key="exact-remote-child",
        source_uri=thread_resource_uri(
            SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gateway_source",
        ),
        target_container_uri=session_resource_uri(
            TARGET_WORKSPACE_ID, TARGET_SESSION_ID, gateway_authority="gateway_target",
        ),
        creation_kind="new_child_thread", mode="full_rollout_copy",
        source_view_id="view-frozen-child", source_view_revision=7,
    )


@pytest.fixture
def fork_result(fork_request: SessionForkRequest) -> SessionForkResultDTO:
    now = datetime.now(UTC)
    return SessionForkResultDTO(
        session=SessionDTO(
            session_id=TARGET_SESSION_ID, workspace_id=TARGET_WORKSPACE_ID,
            resource_identity=session_resource_identity(TARGET_WORKSPACE_ID, TARGET_SESSION_ID),
            uri=session_resource_uri(TARGET_WORKSPACE_ID, TARGET_SESSION_ID),
            scope="workspace", title="远端目标", current_agent_id="default", created_at=now, updated_at=now,
        ),
        thread_id=TARGET_THREAD_ID,
        thread_uri=thread_resource_uri(
            TARGET_WORKSPACE_ID, TARGET_SESSION_ID, TARGET_THREAD_ID, gateway_authority="gateway_target",
        ),
        creation_kind=fork_request.creation_kind, source_uri=fork_request.source_uri,
        source_view_id=fork_request.source_view_id, source_view_revision=fork_request.source_view_revision,
        mode=fork_request.mode,
    )


@pytest.mark.parametrize(
    ("creation_kind", "target_kind"),
    [("new_session_main", "inventory"), ("new_child_thread", "session"), ("new_child_thread", "thread")],
)
async def test_remote_fork_preserves_exact_source_and_published_target(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    creation_kind: SessionForkCreationKind, target_kind: str,
) -> None:
    target_uri = fork_request.target_container_uri
    if target_kind == "inventory":
        target_uri = workspace_session_inventory_uri(TARGET_WORKSPACE_ID, gateway_authority="gateway_target")
    elif target_kind == "thread":
        target_uri = thread_resource_uri(
            TARGET_WORKSPACE_ID, TARGET_SESSION_ID, TARGET_CONTAINER_THREAD_ID, gateway_authority="gateway_target",
        )
    request = fork_request.model_copy(update={"creation_kind": creation_kind, "target_container_uri": target_uri})
    published = fork_result.model_copy(update={"creation_kind": creation_kind})
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    result = await fork_boundary.service.fork(request)

    assert result is published
    assert result.session.uri == session_resource_uri(TARGET_WORKSPACE_ID, TARGET_SESSION_ID)
    assert len(fork_boundary.transport.calls) == 1
    forwarded = fork_boundary.transport.calls[0]
    assert forwarded.action == "create" and forwarded.source_gateway_id == "gateway_origin"
    assert forwarded.request == request
    assert forwarded.fork_target.uri == target_uri and forwarded.fork_target.fork_role == "target"
    assert result.thread_uri != target_uri and result.thread_id != TARGET_CONTAINER_THREAD_ID
    fork_boundary.sessions.resolve_main_thread.assert_not_awaited()
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()


async def test_remote_fork_rejects_self_consistent_child_of_another_session(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
) -> None:
    wrong_session = fork_result.session.model_copy(update={
        "session_id": OTHER_SESSION_ID,
        "resource_identity": session_resource_identity(TARGET_WORKSPACE_ID, OTHER_SESSION_ID),
        "uri": session_resource_uri(TARGET_WORKSPACE_ID, OTHER_SESSION_ID),
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=fork_result.model_copy(update={
        "session": wrong_session,
        "thread_uri": thread_resource_uri(
            TARGET_WORKSPACE_ID, OTHER_SESSION_ID, TARGET_THREAD_ID, gateway_authority="gateway_target",
        ),
    }))

    with pytest.raises(RuntimeError, match="child 不属于固定目标 Session"):
        await fork_boundary.service.fork(fork_request)


async def test_remote_fork_accepts_session_uri_qualified_with_target_gateway(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
) -> None:
    published = fork_result.model_copy(update={
        "session": fork_result.session.model_copy(update={
            "uri": session_resource_uri(TARGET_WORKSPACE_ID, TARGET_SESSION_ID, gateway_authority="gateway_target"),
        }),
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    assert await fork_boundary.service.fork(fork_request) is published
    assert len(fork_boundary.transport.calls) == 1
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("uri", session_resource_uri(TARGET_WORKSPACE_ID, TARGET_SESSION_ID, gateway_authority="gateway_other"), "Session URI"),
        ("uri", session_resource_uri(SOURCE_WORKSPACE_ID, TARGET_SESSION_ID), "Session URI"),
        ("uri", session_resource_uri(TARGET_WORKSPACE_ID, OTHER_SESSION_ID), "Session URI"),
        ("resource_identity", session_resource_identity(SOURCE_WORKSPACE_ID, TARGET_SESSION_ID), "Session resource_identity"),
        ("resource_identity", session_resource_identity(TARGET_WORKSPACE_ID, OTHER_SESSION_ID), "Session resource_identity"),
    ],
    ids=["uri-gateway", "uri-workspace", "uri-session", "identity-workspace", "identity-session"],
)
async def test_remote_fork_rejects_changed_nested_session_identity(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    field: str, value: str, error: str,
) -> None:
    published = fork_result.model_copy(update={
        "session": fork_result.session.model_copy(update={field: value}),
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    with pytest.raises(RuntimeError, match=error):
        await fork_boundary.service.fork(fork_request)
    assert len(fork_boundary.transport.calls) == 1
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()


async def test_remote_fork_rejects_changed_creation_kind(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
) -> None:
    fork_boundary.transport.reply = SessionForkTransferResultDTO(
        result=fork_result.model_copy(update={"creation_kind": "new_session_main"}),
    )
    with pytest.raises(RuntimeError, match="创建意图与固定请求不一致"):
        await fork_boundary.service.fork(fork_request)


@pytest.mark.parametrize(
    ("changed", "error"),
    [
        ({"source_view_id": "view-other"}, "来源 view/revision"),
        ({"source_view_revision": 8}, "来源 view/revision"),
        ({"anchor_item_id": "item-other"}, "来源 anchor"),
        ({"mode": "context_fork"}, "mode 与固定请求"),
    ],
    ids=["view", "revision", "anchor", "mode"],
)
async def test_remote_fork_rejects_a_different_published_source_point(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    changed: dict[str, object], error: str,
) -> None:
    request = SessionForkRequest.model_validate({
        **fork_request.model_dump(mode="json"), "mode": "view_prefix_fork", "anchor_item_id": "item-selected",
    })
    published = SessionForkResultDTO.model_validate({
        **fork_result.model_dump(mode="json"), "mode": request.mode, "anchor_item_id": request.anchor_item_id,
        **changed,
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    with pytest.raises(RuntimeError, match=error):
        await fork_boundary.service.fork(request)
    assert len(fork_boundary.transport.calls) == 1
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()


@pytest.mark.parametrize(
    "source_uri",
    [
        thread_resource_uri(SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gateway_other"),
        thread_resource_uri(TARGET_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gateway_source"),
        thread_resource_uri(SOURCE_WORKSPACE_ID, OTHER_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gateway_source"),
        thread_resource_uri(SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, OTHER_THREAD_ID, gateway_authority="gateway_source"),
        session_resource_uri(SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, gateway_authority="gateway_source"),
        thread_resource_uri(SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_THREAD_ID),
    ],
    ids=["gateway", "workspace", "session", "thread", "session-only", "unqualified"],
)
async def test_remote_fork_rejects_a_different_published_source_owner(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    source_uri: str,
) -> None:
    published = SessionForkResultDTO.model_validate({**fork_result.model_dump(mode="json"), "source_uri": source_uri})
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    with pytest.raises(RuntimeError, match="来源 URI 与固定请求不一致"):
        await fork_boundary.service.fork(fork_request)


@pytest.mark.parametrize(
    "changed", [{"source_uri": "boxteam://invalid"}, {"thread_id": "invalid%thread"}],
    ids=["source-uri", "target-thread"],
)
async def test_remote_fork_classifies_malformed_response_identity_as_protocol_failure(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    changed: dict[str, object],
) -> None:
    published = SessionForkResultDTO.model_validate({**fork_result.model_dump(mode="json"), **changed})
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    with pytest.raises(RuntimeError, match="fork_response_protocol_invalid") as caught:
        await fork_boundary.service.fork(fork_request)
    assert isinstance(caught.value.__cause__, ValueError)
    assert not isinstance(caught.value, WorkspaceSessionContextAccessError)
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()


async def test_remote_fork_keeps_invalid_target_container_as_input_failure(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest,
) -> None:
    request = SessionForkRequest.model_validate({
        **fork_request.model_dump(mode="json"),
        "target_container_uri": workspace_session_inventory_uri(TARGET_WORKSPACE_ID, gateway_authority="gateway_target"),
    })

    with pytest.raises(ValueError, match="Session/Thread URI 必须精确"):
        await fork_boundary.service.fork(request)
    assert fork_boundary.transport.calls == []


async def test_remote_session_source_accepts_source_owner_main_thread(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
) -> None:
    request = SessionForkRequest.model_validate({
        **fork_request.model_dump(mode="json"),
        "source_uri": session_resource_uri(SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, gateway_authority="gateway_source"),
    })
    published = SessionForkResultDTO.model_validate({
        **fork_result.model_dump(mode="json"),
        "source_uri": thread_resource_uri(
            SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_MAIN_THREAD_ID, gateway_authority="gateway_source",
        ),
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    result = await fork_boundary.service.fork(request)
    assert result is published
    assert result.source_uri == thread_resource_uri(
        SOURCE_WORKSPACE_ID, SOURCE_SESSION_ID, SOURCE_MAIN_THREAD_ID, gateway_authority="gateway_source",
    )
    assert fork_boundary.transport.calls[0].request.source_uri == request.source_uri
    fork_boundary.sessions.resolve_main_thread.assert_not_awaited()


@pytest.mark.parametrize("resolved_anchor", [None, "item-safe-completion"])
async def test_remote_fork_preserves_source_owner_default_context_anchor(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    resolved_anchor: str | None,
) -> None:
    request = SessionForkRequest.model_validate({**fork_request.model_dump(mode="json"), "mode": "context_fork"})
    published = SessionForkResultDTO.model_validate({
        **fork_result.model_dump(mode="json"), "mode": request.mode, "anchor_item_id": resolved_anchor,
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    assert await fork_boundary.service.fork(request) is published


@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
async def test_remote_fork_rejects_changed_explicit_or_full_copy_anchor(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    mode: str,
) -> None:
    request = SessionForkRequest.model_validate({
        **fork_request.model_dump(mode="json"), "mode": mode,
        "anchor_item_id": "item-selected" if mode == "context_fork" else None,
    })
    published = SessionForkResultDTO.model_validate({
        **fork_result.model_dump(mode="json"), "mode": request.mode, "anchor_item_id": "item-other",
    })
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=published)

    with pytest.raises(RuntimeError, match="来源 anchor 与固定请求不一致"):
        await fork_boundary.service.fork(request)


@pytest.mark.parametrize("missing_result", [False, True])
async def test_remote_fork_requires_publication_in_fixed_workspace(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
    missing_result: bool,
) -> None:
    wrong_workspace = fork_result.session.model_copy(update={"workspace_id": SOURCE_WORKSPACE_ID})
    fork_boundary.transport.reply = SessionForkTransferResultDTO(
        result=None if missing_result else fork_result.model_copy(update={"session": wrong_workspace}),
    )
    with pytest.raises(RuntimeError, match="固定目标 Workspace 的真实发布结果"):
        await fork_boundary.service.fork(fork_request)


async def test_remote_fork_rejects_thread_uri_that_disagrees_with_published_identity(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest, fork_result: SessionForkResultDTO,
) -> None:
    fork_boundary.transport.reply = SessionForkTransferResultDTO(result=fork_result.model_copy(update={
        "thread_uri": thread_resource_uri(
            TARGET_WORKSPACE_ID, TARGET_SESSION_ID, SOURCE_THREAD_ID, gateway_authority="gateway_target",
        ),
    }))
    with pytest.raises(RuntimeError, match="Thread URI 与固定目标身份不一致"):
        await fork_boundary.service.fork(fork_request)


async def test_remote_fork_exposes_unreachable_target_without_local_creation(
    fork_boundary: _ForkBoundary, fork_request: SessionForkRequest,
) -> None:
    failure = WorkspaceSessionContextAccessError("federation-channel-closed: 固定目标不可达")
    fork_boundary.transport.error = failure
    with pytest.raises(WorkspaceSessionContextAccessError) as error:
        await fork_boundary.service.fork(fork_request)
    assert error.value is failure
    assert len(fork_boundary.transport.calls) == 1
    fork_boundary.main_owner.assert_not_awaited()
    fork_boundary.child_owner.assert_not_awaited()
