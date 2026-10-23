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
        response = httpx.Response(
            status if rejected else 200, request=request,
            **({"text": body} if rejected and isinstance(body, str) else {"json": body if rejected else {"request_id": REQUEST_ID, "data": valid}}),
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
