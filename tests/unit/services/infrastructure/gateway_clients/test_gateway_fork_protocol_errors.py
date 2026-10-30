"""Gateway fork 回执的畸形原生位置只进入诊断，原请求仍可重试。"""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferRequest
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    IncompleteForkSourceCaptureError,
)
from tests.contracts.api.test_session_fork_uri_contract import (
    INVALID_ADDRESSES,
)
from tests.contracts.api.test_session_fork_uri_contract import (
    fork_output as fork_output,  # noqa: PLC0414
)
from tests.contracts.api.test_session_fork_uri_contract import (
    fork_output_request as fork_output_request,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_capture_request as gateway_capture_request,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_clients as gateway_clients,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_fork_request as gateway_fork_request,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    incomplete_capture_wire as incomplete_capture_wire,  # noqa: PLC0414
)


@pytest.mark.parametrize("native_path", [
    "/private/fork/capture.json", r"C:\private\fork\capture.json", r"\\private\fork\capture.json",
])
@pytest.mark.parametrize("incomplete", [False, True], ids=["success-receipt", "incomplete-receipt"])
async def test_gateway_fork_protocol_error_keeps_raw_input_in_diagnostics(
    gateway_clients: list[GatewaySessionContextClient],
    gateway_capture_request: SessionForkTransferRequest,
    incomplete_capture_wire: dict[str, object],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    native_path: str,
    incomplete: bool,
) -> None:
    request = gateway_capture_request if incomplete else gateway_capture_request.model_copy(
        update={"action": "intent", "capture": None},
    )
    frozen_request = request.model_dump(mode="json")
    invalid_input = [{"native_path": native_path}]
    payload = copy.deepcopy(incomplete_capture_wire) if incomplete else {
        "request_id": "req_gateway_fork", "data": {"intent": invalid_input},
    }
    if incomplete:
        payload["detail"]["detail"]["capture"]["snapshot_id"] = invalid_input
    wire_requests: list[dict[str, object]] = []

    async def respond(
        _client: httpx.AsyncClient, method: str, url: str, *, headers: dict[str, str],
        json: dict[str, object], **_kwargs: object,
    ) -> httpx.Response:
        wire_requests.append({"method": method, "url": url, "headers": headers, "body": json})
        return httpx.Response(
            409 if incomplete else 200, request=httpx.Request(method, url, headers=headers),
            headers={"X-Request-ID": headers["X-Request-ID"]}, json=payload,
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-protocol-test-token")
    gateway_clients.append(client)
    with pytest.raises(RuntimeError, match="fork_response_protocol_invalid") as caught:
        await client.fork_transfer(request)
    assert not isinstance(caught.value, (WorkspaceSessionContextAccessError, IncompleteForkSourceCaptureError))
    original_error = caught.value.__cause__
    assert isinstance(original_error, ValidationError)
    assert original_error.errors()[0]["input"] == invalid_input
    public = json.dumps({"error": str(caught.value)})
    assert "native_path" not in public and "private" not in public
    stage = "incomplete_capture_validation" if incomplete else "response_validation"
    for value in ("req_gateway_fork", request.fork_target.uri, f"action={request.action}",
                  f"stage={stage}", "error_type=ValidationError"):
        assert value in str(caught.value)
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert request.model_dump(mode="json") == frozen_request

    assert len(wire_requests) == 1

    # 只修复 wire 回执；原 request、来源/目标与请求身份保持原值，不冒充已提交 capture。
    payload = incomplete_capture_wire if incomplete else {
        "request_id": "req_gateway_fork", "data": {"intent": {"request": frozen_request["request"]}},
    }
    if incomplete:
        with pytest.raises(IncompleteForkSourceCaptureError) as retried:
            await client.fork_transfer(request)
        assert retried.value.capture == request.capture.to_record()
        assert retried.value.__cause__ is None
    else:
        reply = await client.fork_transfer(request)
        assert reply.intent == payload["data"]["intent"]
    assert wire_requests[0] == wire_requests[1]
    assert request.model_dump(mode="json") == frozen_request


@pytest.mark.parametrize("address", INVALID_ADDRESSES)
@pytest.mark.parametrize("location", [
    ("session", "uri"), ("thread_uri",), ("source_uri",),
    ("resources", 0, "resource_uri"), ("resources", 0, "target_uri"),
])
async def test_gateway_fork_rejects_invalid_result_addresses(
    gateway_clients, fork_output_request, fork_output, location, address, caplog, monkeypatch,
):
    node = fork_output
    for key in location[:-1]:
        node = node[key]
    original = node[location[-1]]
    node[location[-1]] = address
    wire_requests = []

    async def respond(_client, method, url, *, headers, json, **_kwargs):
        wire_requests.append(json)
        return httpx.Response(
            200, request=httpx.Request(method, url, headers=headers),
            headers={"X-Request-ID": headers["X-Request-ID"]},
            json={"request_id": headers["X-Request-ID"], "data": {"result": fork_output}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-protocol-test-token")
    gateway_clients.append(client)
    frozen_request = fork_output_request.model_dump(mode="json")
    with pytest.raises(RuntimeError, match="fork_response_protocol_invalid") as caught:
        await client.fork_transfer(fork_output_request)
    cause = caught.value.__cause__
    assert isinstance(cause, ValidationError)
    assert [(item["loc"], item["input"]) for item in cause.errors()] == [(("result", *location), address)]
    assert address not in str(caught.value)
    assert "req_gateway_context" in str(caught.value)
    assert "action=create" in str(caught.value)
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert len(wire_requests) == 1

    node[location[-1]] = original
    reply = await client.fork_transfer(fork_output_request)
    assert reply.result is not None
    assert reply.result.model_dump(mode="json") == fork_output
    assert len(wire_requests) == 2 and wire_requests[0] == wire_requests[1]
    assert fork_output_request.model_dump(mode="json") == frozen_request
