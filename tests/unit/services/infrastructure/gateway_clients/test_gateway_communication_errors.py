"""通信 HTTP 拒绝不复制原始正文，保留 observation 和恢复分类。"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import asdict

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.session_control_communication_ledger.observations import (
    OBSERVATION_ERROR_CODES,
    RemoteObservationError,
)
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from tests.support.communication import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    WORKSPACE_ID,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_communication_client import (
    GATEWAY_ID,
    REQUEST_ID,
    ReadyResponses,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_communication_client import (
    ready_responses as ready_responses,  # noqa: PLC0414
)


@pytest.fixture
def communication_invocation(
    request: pytest.FixtureRequest, ready_responses: ReadyResponses,
) -> tuple[Callable[[], Awaitable[object]], dict[str, object]]:
    client = GatewayCommunicationClient(gateway_url="http://communication-errors.test")
    target = GlobalThreadAddress(GATEWAY_ID, WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD)
    if request.param == "discovery":
        return (lambda: client.resolve_main(source=target, session_id=SOURCE_SESSION)), {
            "target": asdict(target), "main_thread_id": SOURCE_THREAD,
        }
    return (lambda: client.operation(operation=request.param, target=target, body={"operation_id": "original-operation"})), {
        "original_result": True,
    }


@pytest.mark.parametrize("communication_invocation", ["discovery", "send", "read", "wait"], indirect=True)
@pytest.mark.parametrize("status,body,content_type,code", [
    (404, {"detail": "workspace not found: /private/communication/workspace"}, "application/json", None),
    (409, {"detail": {
        "code": "federation-target-owner-rejected", "message": r"C:\private\communication\owner",
        "detail": {"owner": "unknown_owner", "code": "/private/communication/code"},
    }}, "application/json", "federation-target-owner-rejected"),
    (500, r"<html>\\private\communication\server</html>", "text/html", None),
    (503, {"detail": {"code": "/private/communication/unknown"}}, "application/json", None),
    (409, ["/private/communication/malformed"], "application/json", None),
    (502, '{"detail":"/private/communication/truncated', "application/json", None),
])
async def test_communication_http_error_keeps_request_and_classification_without_raw_body(
    communication_invocation: tuple[Callable[[], Awaitable[object]], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    status: int, body: object, content_type: str, code: str | None,
) -> None:
    invoke, valid = communication_invocation
    requests: list[dict[str, object]] = []
    responses: list[httpx.Response] = []
    rejected = True

    async def respond(client, method, path, **kwargs):
        requests.append(copy.deepcopy({
            "method": method, "url": str(client.base_url.join(path)),
            "headers": dict(client.headers), "json": kwargs["json"],
            "timeout": client.timeout.as_dict(),
        }))
        request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
        error_body = {**body, "request_id": REQUEST_ID} if isinstance(body, dict) else body
        response = httpx.Response(
            status if rejected else 200, request=request,
            **({"text": body} if rejected and isinstance(body, str) else {"json": error_body if rejected else {"request_id": REQUEST_ID, "data": valid}}),
            headers={"X-Request-ID": REQUEST_ID, "Content-Type": content_type},
        )
        responses.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    with pytest.raises(WorkspaceSessionContextAccessError) as caught:
        await invoke()
    public = str(caught.value)
    assert "private" not in public
    assert "stage=http_response" in public and f"status={status}" in public
    assert f"request_id={REQUEST_ID}" in public and caught.value.__cause__ is None
    assert not isinstance(caught.value, RemoteObservationError)
    if code:
        assert f"code={code}" in public
    else:
        assert ", code=" not in public
    assert any(responses[0].text[:2000] in record.getMessage() for record in caplog.records)
    rejected = False
    result = await invoke()
    assert result is not None
    assert requests[0] == requests[1]


@pytest.mark.parametrize("communication_invocation", ["read", "wait"], indirect=True)
@pytest.mark.parametrize("code", sorted(OBSERVATION_ERROR_CODES))
async def test_communication_observation_rejections_preserve_original_public_code(
    communication_invocation: tuple[Callable[[], Awaitable[object]], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: str,
) -> None:
    requests: list[dict[str, object]] = []
    rejected = True

    async def respond(client, method, path, **kwargs):
        requests.append(copy.deepcopy({
            "method": method, "url": str(client.base_url.join(path)),
            "headers": dict(client.headers), "json": kwargs["json"],
            "timeout": client.timeout.as_dict(),
        }))
        return httpx.Response(409 if rejected else 200, request=httpx.Request(method, client.base_url.join(path)), json={
            "request_id": REQUEST_ID,
            "detail": {"code": "federation-target-owner-rejected", "detail": {
                "owner": "communication_observation", "code": code, "message": "/private/communication/observation",
            }},
        } if rejected else {"request_id": REQUEST_ID, "data": communication_invocation[1]}, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    with pytest.raises(RemoteObservationError) as caught:
        await communication_invocation[0]()
    assert caught.value.code == code and str(caught.value) == code
    assert caught.value.__cause__ is None
    assert any("/private/communication/observation" in record.getMessage() for record in caplog.records)
    rejected = False
    assert await communication_invocation[0]() == communication_invocation[1]
    assert requests[0] == requests[1]


@pytest.mark.parametrize("communication_invocation", ["discovery", "send", "read", "wait"], indirect=True)
@pytest.mark.parametrize("identity", ["valid", "body_drift", "header_drift", "body_missing", "header_missing"])
async def test_communication_error_codes_require_matching_response_identity(
    communication_invocation: tuple[Callable[[], Awaitable[object]], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, identity: str,
) -> None:
    responses: list[httpx.Response] = []

    async def respond(client, method, path, **kwargs):
        payload = {
            "request_id": "other" if identity == "body_drift" else REQUEST_ID,
            "detail": {"code": "federation-target-owner-rejected", "detail": {
                "owner": "communication_observation", "code": "read-snapshot-invalid",
                "message": "/private/communication/identity",
            }},
        }
        headers = {"X-Request-ID": "other" if identity == "header_drift" else REQUEST_ID}
        if identity == "body_missing":
            del payload["request_id"]
        if identity == "header_missing":
            del headers["X-Request-ID"]
        request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
        assert request.headers["X-Request-ID"] == REQUEST_ID
        response = httpx.Response(409, request=request, json=payload, headers=headers)
        responses.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    expected_error = RemoteObservationError if identity == "valid" else WorkspaceSessionContextAccessError
    with pytest.raises(expected_error) as caught:
        await communication_invocation[0]()
    if identity == "valid":
        assert isinstance(caught.value, RemoteObservationError)
        assert caught.value.code == "read-snapshot-invalid"
    else:
        assert type(caught.value) is WorkspaceSessionContextAccessError
        assert "stage=http_response, status=409" in str(caught.value)
        assert "read-snapshot-invalid" not in str(caught.value)
        assert "federation-target-owner-rejected" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert any(responses[0].text[:2000] in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("communication_invocation", ["discovery", "send", "read", "wait"], indirect=True)
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
@pytest.mark.parametrize("native_path", ["/private/communication/socket", r"C:\private\communication\socket", r"\\server\private\communication\socket"])
@pytest.mark.parametrize("with_request", [True, False])
async def test_communication_connection_failure_preserves_original_type_request_and_retry(
    communication_invocation: tuple[Callable[[], Awaitable[object]], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failure_type: type[httpx.RequestError], native_path: str, with_request: bool,
) -> None:
    invoke, valid = communication_invocation
    requests: list[dict[str, object]] = []
    failures: list[httpx.RequestError] = []
    rejected = True

    async def respond(client, method, path, **kwargs):
        requests.append(copy.deepcopy({
            "method": method, "url": str(client.base_url.join(path)),
            "headers": dict(client.headers), "json": kwargs["json"],
            "timeout": client.timeout.as_dict(),
        }))
        request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
        if rejected:
            failure = failure_type("原生连接失败: " + native_path, request=request if with_request else None)
            failures.append(failure)
            raise failure
        return httpx.Response(200, request=request, json={"request_id": REQUEST_ID, "data": valid}, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    with pytest.raises(failure_type) as caught:
        await invoke()
    assert type(caught.value) is failure_type and caught.value.__cause__ is failures[0]
    assert native_path in str(failures[0])
    if with_request:
        assert caught.value.request is failures[0].request
    else:
        with pytest.raises(RuntimeError, match="request property has not been set"):
            _ = caught.value.request
    public = str(caught.value)
    assert native_path not in public and "private" not in public
    assert f"request_id={REQUEST_ID}, stage=connection, error_type={failure_type.__name__}" in public
    assert "path=" + httpx.URL(requests[0]["url"]).path in public
    assert any(record.exc_info and record.exc_info[1] is failures[0] for record in caplog.records)
    rejected = False
    assert await invoke() is not None
    assert requests[0] == requests[1]
