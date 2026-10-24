"""传输拒绝保留原分类与诊断，普通工具错误不复制原生路径。"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.internal_v2.session_context import SessionContextSearchRequest
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferRequest
from app.services.infrastructure.gateway_client_common import (
    MODEL_RECOVERABLE_HTTP_STATUSES,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    _read_request,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_clients as gateway_clients,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_fork_request as gateway_fork_request,  # noqa: PLC0414
)


@pytest.fixture
def context_operation(
    request: pytest.FixtureRequest, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest,
) -> Callable[[], Awaitable[object]]:
    client = GatewaySessionContextClient(local_token_supplier=lambda: "transport-test")
    gateway_clients.append(client)
    read = _read_request("ws_query")
    if request.param == "read":
        return lambda: client.read_resource_context(read)
    if request.param == "search":
        search = SessionContextSearchRequest(
            **read.model_dump(include={"resource_identity", "uri", "scope"}), query="needle",
        )
        return lambda: client.search_resource_context(search)
    if request.param == "inventory":
        return client.list_gateway_workspaces
    assert request.param == "fork"
    return lambda: client.fork_transfer(gateway_fork_request)


@pytest.mark.parametrize("context_operation,native_path", [
    ("read", "/private/context/connection.sock"),
    ("search", r"C:\private\context\connection.sock"),
    ("fork", r"\\private\context\connection.sock"),
], indirect=["context_operation"])
async def test_connection_failure_keeps_original_cause_and_request_without_public_path(
    context_operation: Callable[[], Awaitable[object]], native_path: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    failure = httpx.ConnectError(f"Cannot open {native_path}")
    requests: list[dict[str, object]] = []

    async def fail(_client, method, url, **kwargs):
        requests.append(copy.deepcopy({"method": method, "url": url, **kwargs}))
        raise failure

    monkeypatch.setattr(httpx.AsyncClient, "request", fail)
    for _ in range(2):
        with pytest.raises(WorkspaceSessionContextAccessError) as caught:
            await context_operation()
        public = str(caught.value)
        assert native_path not in public and "private" not in public
        assert "stage=connection" in public and "error_type=ConnectError" in public
        assert "path=/api/gateway/federation/relay" in public and "request_id=" in public
        assert caught.value.__cause__ is failure
    assert requests[0] == requests[1]
    assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)


@pytest.mark.parametrize("context_operation", ["read", "fork", "inventory"], indirect=True)
@pytest.mark.parametrize("status,body,expected_codes", [
    (404, {"detail": "Gateway 工作区不存在: /private/context/workspace"}, ()),
    (409, {"detail": {
        "code": "federation-target-owner-rejected", "message": r"C:\private\context\response",
        "detail": {"owner": "resource_uri_resolver", "code": "snapshot_unavailable", "path": "/private/context"},
    }}, ("federation-target-owner-rejected", "owner=resource_uri_resolver", "owner_code=snapshot_unavailable")),
    (409, {"detail": {
        "code": "federation-target-owner-rejected", "message": "/private/context",
        "detail": {"owner": "context_revision", "code": "snapshot_changed", "actual_revision": "/private/context"},
    }}, ("federation-target-owner-rejected", "owner=context_revision", "owner_code=snapshot_changed")),
    (500, {"detail": {"code": "federation-channel-closed", "message": "/private/context"}}, ("federation-channel-closed",)),
    (503, "<html>failed to open /private/context/response</html>", ()),
    (409, {"detail": {"code": "/private/context/code", "detail": {"owner": "/private/context"}}}, ()),
    (409, {"detail": {"code": "federation-target-owner-rejected", "detail": {
        "owner": "resource_uri_resolver", "code": "/private/context/unknown",
    }}}, ("federation-target-owner-rejected",)),
    (409, {"detail": {"code": "federation-target-owner-rejected", "detail": {
        "owner": "/private/context/owner", "code": "snapshot_changed",
    }}}, ("federation-target-owner-rejected",)),
    (307, ["/private/context/redirect"], ()),
])
async def test_http_failure_preserves_status_and_registered_codes_with_raw_log_only(
    context_operation: Callable[[], Awaitable[object]], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, status: int, body: object, expected_codes: tuple[str, ...],
) -> None:
    responses: list[httpx.Response] = []

    async def respond(_client, method, url, **kwargs):
        request_id = kwargs["headers"].get("X-Request-ID")
        error_body = {**body, "request_id": request_id} if isinstance(body, dict) else body
        response = httpx.Response(
            status, request=httpx.Request(method, url, headers=kwargs["headers"]),
            **({"text": body} if isinstance(body, str) else {"json": error_body}),
            headers={"X-Request-ID": request_id} if request_id is not None else {},
        )
        responses.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    with pytest.raises(RuntimeError) as caught:
        await context_operation()
    public = str(caught.value)
    assert "private" not in public
    assert f"status={status}" in public and "stage=http_response" in public
    assert "request_id=" in public and "fork_response_protocol_invalid" not in public
    assert isinstance(caught.value, WorkspaceSessionContextAccessError) is (status in MODEL_RECOVERABLE_HTTP_STATUSES)
    assert caught.value.__cause__ is None
    if "X-Request-ID" not in responses[0].request.headers:
        expected_codes = ()  # 未绑定调用身份的 inventory 请求不接受远端业务错误码。
    assert all(code in public for code in expected_codes)
    if not expected_codes:
        assert ", code=" not in public
    if not any(code.startswith("owner=") for code in expected_codes):
        assert ", owner=" not in public and "owner_code=" not in public
    if status == 404:
        assert "Gateway 工作区不存在或目标资源不可访问" in public
    assert any(responses[0].text[:2000] in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("context_operation", ["read", "search", "fork", "inventory"], indirect=True)
@pytest.mark.parametrize("owner,code", [
    ("resource_uri_resolver", "snapshot_unavailable"),
    ("context_revision", "snapshot_changed"),
])
@pytest.mark.parametrize("identity", ["valid", "body_drift", "header_drift", "body_missing", "header_missing"])
async def test_context_error_codes_require_matching_response_identity(
    context_operation: Callable[[], Awaitable[object]], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, owner: str, code: str, identity: str,
) -> None:
    responses: list[httpx.Response] = []

    async def respond(_client, method, url, **kwargs):
        request_id = kwargs["headers"].get("X-Request-ID")
        payload = {
            "request_id": "other" if identity == "body_drift" else request_id,
            "detail": {"code": "federation-target-owner-rejected", "detail": {
                "owner": owner, "code": code, "message": "/private/context/identity",
            }},
        }
        headers = {"X-Request-ID": "other" if identity == "header_drift" else request_id} if request_id is not None else {}
        if identity == "body_missing":
            del payload["request_id"]
        if identity == "header_missing":
            headers.pop("X-Request-ID", None)
        response = httpx.Response(409, request=httpx.Request(method, url, headers=kwargs["headers"]), json=payload, headers=headers)
        responses.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    with pytest.raises(WorkspaceSessionContextAccessError) as caught:
        await context_operation()
    public = str(caught.value)
    if identity == "valid" and "X-Request-ID" in responses[0].request.headers:
        assert f", owner={owner}, owner_code={code}" in public
        assert ", code=federation-target-owner-rejected" in public
    else:
        assert ", code=" not in public and ", owner=" not in public and "owner_code=" not in public
    assert "private" not in public and "stage=http_response, status=409" in public
    assert any(responses[0].text[:2000] in record.getMessage() for record in caplog.records)
