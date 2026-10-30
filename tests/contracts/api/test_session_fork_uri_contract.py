"""fork 公开输出只接受资源 URI；合法历史地址保持原字节。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.deps import (
    get_session_context_fork_service,
    get_session_service,
    verify_local_token,
)
from app.api.sessions import router
from app.core.resource_uri import thread_resource_uri
from app.core.trace_middleware import TraceMiddleware
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionForkRequest,
    SessionForkResourceResult,
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferRequest
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_uri,
)
from tests.support.communication.probe import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)

GATEWAY_ID = "gateway_3097a8378d6e2c98a52a687e4a004769"
FILE_URI = f"boxteam://{GATEWAY_ID}/workspace/{WORKSPACE_ID}/resources/files/root/%e6%96%87%20%E4%BB%B6.txt"
INVALID_ADDRESSES = [
    "/private/fork/file.txt", r"C:\private\fork\file.txt", r"\\host\share\file.txt",
    "relative/file.txt", "file:///private/fork/file.txt", "boxteam://invalid",
]


@pytest.fixture
def fork_output_request() -> SessionForkTransferRequest:
    target_uri = session_resource_uri(WORKSPACE_ID, TARGET_SESSION, gateway_authority=GATEWAY_ID)
    return SessionForkTransferRequest(
        source_gateway_id=GATEWAY_ID, action="create",
        fork_target=ForkGrantTarget(gateway_id=GATEWAY_ID, uri=target_uri, fork_role="target"),
        request=SessionForkRequest(
            idempotency_key="fork-output-contract",
            source_uri=thread_resource_uri(WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD, gateway_authority=GATEWAY_ID),
            target_container_uri=target_uri, mode="full_rollout_copy",
            source_view_id="view-frozen", source_view_revision=7,
        ),
    )


@pytest.fixture
def fork_output(fork_output_request: SessionForkTransferRequest):
    request = fork_output_request.request
    assert request.source_view_id is not None and request.source_view_revision is not None
    now = datetime(2026, 10, 30, tzinfo=UTC)
    return SessionForkResultDTO(
        session=SessionDTO(
            session_id=TARGET_SESSION, workspace_id=WORKSPACE_ID,
            resource_identity=session_resource_identity(WORKSPACE_ID, TARGET_SESSION),
            uri=request.target_container_uri, scope="workspace", title="/ordinary/session-title",
            current_agent_id="default", created_at=now, updated_at=now,
        ),
        thread_id=TARGET_THREAD,
        thread_uri=thread_resource_uri(WORKSPACE_ID, TARGET_SESSION, TARGET_THREAD, gateway_authority=GATEWAY_ID),
        target_gateway_id=GATEWAY_ID, creation_kind=request.creation_kind,
        source_uri=request.source_uri, source_view_id=request.source_view_id,
        source_view_revision=request.source_view_revision, mode=request.mode,
        resources=[SessionForkResourceResult(
            resource_uri=FILE_URI, target_uri=FILE_URI, action="copy_snapshot",
            availability="available", reason="/ordinary/resource-description",
        )],
    ).model_dump(mode="json")


@pytest.fixture
def fork_source_output(fork_output_request: SessionForkTransferRequest):
    return {
        "source_uri": fork_output_request.request.source_uri,
        "source_view_id": "view-frozen", "source_view_revision": 7, "anchor_item_id": None,
        "child_container_uri": session_resource_uri(WORKSPACE_ID, SOURCE_SESSION),
        "main_container_uri": workspace_session_inventory_uri(WORKSPACE_ID),
        "title": "/ordinary/branch-title", "goal_available": False,
        "resource_mode": "full_rollout_copy", "resource_anchor_mode": "inclusive",
        "resources": [{"resource_uri": FILE_URI, "name": "文 件.txt", "content_type": "text/plain",
                       "availability": "unavailable", "required_for_context": True}],
    }


@pytest.mark.parametrize("address", INVALID_ADDRESSES)
@pytest.mark.parametrize("location", [
    ("session", "uri"), ("thread_uri",), ("source_uri",),
    ("resources", 0, "resource_uri"), ("resources", 0, "target_uri"),
])
def test_fork_result_rejects_invalid_output_address(fork_output, location, address):
    node = fork_output
    for key in location[:-1]:
        node = node[key]
    node[location[-1]] = address
    with pytest.raises(ValidationError) as caught:
        SessionForkResultDTO.model_validate(fork_output)
    assert [(item["loc"], item["input"]) for item in caught.value.errors()] == [(location, address)]


@pytest.mark.parametrize("address", INVALID_ADDRESSES)
@pytest.mark.parametrize("location", [
    ("source_uri",), ("child_container_uri",), ("main_container_uri",), ("resources", 0, "resource_uri"),
])
def test_fork_source_rejects_invalid_output_address(fork_source_output, location, address):
    node = fork_source_output
    for key in location[:-1]:
        node = node[key]
    node[location[-1]] = address
    with pytest.raises(ValidationError) as caught:
        SessionForkSourcePointDTO.model_validate(fork_source_output)
    assert [(item["loc"], item["input"]) for item in caught.value.errors()] == [(location, address)]


def test_fork_output_preserves_opaque_uris_nullable_references_and_text(fork_output, fork_source_output):
    result = SessionForkResultDTO.model_validate(fork_output)
    assert result.model_dump(mode="json") == fork_output
    assert result.resources[0].resource_uri == FILE_URI and result.resources[0].target_uri == FILE_URI
    assert SessionForkSourcePointDTO.model_validate(fork_source_output).model_dump(mode="json", exclude={"live_resources"}) == fork_source_output
    nullable = {"resource_uri": None, "target_uri": None, "action": "omit", "availability": "unavailable"}
    resource = SessionForkResourceResult.model_validate(nullable)
    assert resource.resource_uri is None and resource.target_uri is None


@pytest.fixture
async def output_error_api():
    operation = AsyncMock()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(TraceMiddleware)
    app.dependency_overrides[verify_local_token] = lambda: "local"
    app.dependency_overrides[get_session_service] = lambda: SimpleNamespace(create=operation, list=operation)
    app.dependency_overrides[get_session_context_fork_service] = lambda: SimpleNamespace(
        fork=operation, transfer=operation, source_point=operation,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace.test") as client:
        yield client, operation


@pytest.mark.parametrize("endpoint", ["create", "list", "fork", "source", "transfer"])
@pytest.mark.parametrize("address", INVALID_ADDRESSES[:3])
async def test_internal_output_validation_is_safe_server_failure(
    output_error_api, fork_output_request, fork_output, endpoint, address, caplog,
):
    client, operation = output_error_api
    fork_output["resources"][0]["resource_uri"] = address
    with pytest.raises(ValidationError) as invalid:
        SessionForkResultDTO.model_validate(fork_output)
    operation.side_effect = invalid.value
    prefix = "/api/v1/sessions"
    routes = {
        "create": ("POST", prefix, {"title": "test"}),
        "list": ("GET", prefix, None),
        "fork": ("POST", f"{prefix}/{SOURCE_SESSION}/fork-context", fork_output_request.request.model_dump(mode="json")),
        "source": ("GET", f"{prefix}/{SOURCE_SESSION}/fork-source-point", None),
        "transfer": ("POST", f"{prefix}/fork-transfer", fork_output_request.model_dump(mode="json")),
    }
    method, path, body = routes[endpoint]
    headers = {"X-Request-ID": "req_fork_output_error", "X-BoxTeam-Gateway-Id": GATEWAY_ID}
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req_fork_output_error"
    assert response.json()["request_id"] == "req_fork_output_error"
    assert "ValidationError" in response.json()["message"]
    assert address not in response.text and "private" not in response.text
    assert any(record.exc_info and record.exc_info[1] is invalid.value for record in caplog.records)
    assert invalid.value.errors()[0]["input"] == address
    assert operation.await_count == 1

    # 仅内部 DTO 失败由全局出口处理，普通业务输入错误保持原状态码和文本。
    operation.side_effect = ValueError("fork_input_invalid")
    retry = await client.request(method, path, headers=headers, json=body)
    assert retry.status_code == (409 if endpoint == "list" else 400)
    assert retry.json()["detail"] == "fork_input_invalid"
    assert operation.await_count == 2
