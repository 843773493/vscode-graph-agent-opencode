"""上下文客户端畸形回执只向普通错误提供协议坐标，保留原件供诊断。"""

from __future__ import annotations

import copy
from json import JSONDecodeError

import httpx
import pytest
from pydantic import ValidationError

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.internal_v2.session_context import SessionContextSearchRequest
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    _read_request,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_session_context_client import (
    gateway_clients as gateway_clients,  # noqa: PLC0414
)


@pytest.mark.parametrize("operation,native_path", [
    ("read", "/private/context/response.json"),
    ("search", r"C:\private\context\response.json"),
    ("inventory", r"\\private\context\response.json"),
])
async def test_context_response_validation_keeps_original_input_in_diagnostics(
    gateway_clients: list[GatewaySessionContextClient], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, operation: str, native_path: str,
) -> None:
    read = _read_request("ws_query")
    search = SessionContextSearchRequest(**read.model_dump(include={"resource_identity", "uri", "scope"}), query="needle")
    valid = ({"active_workspace_id": None, "items": []} if operation == "inventory" else {
        **read.model_dump(include={"resource_identity", "uri", "scope"}), "revision": "rev-1",
        **({"view": "overview"} if operation == "read" else {"query": "needle", "match_mode": "literal"}),
    })
    field = "active_workspace_id" if operation == "inventory" else "revision"
    invalid = {"native_path": native_path}
    data = {**valid, field: invalid}
    wire_requests: list[dict[str, object]] = []

    async def respond(_client, method, url, **kwargs):
        wire_requests.append(copy.deepcopy({"method": method, "url": url, **kwargs}))
        request_id = kwargs["headers"].get("X-Request-ID")
        return httpx.Response(200, request=httpx.Request(method, url),
            headers={"X-Request-ID": request_id} if request_id else {},
            json={"request_id": request_id, "data": data})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "context-protocol-test")
    gateway_clients.append(client)

    async def invoke():
        if operation == "inventory":
            return await client.list_gateway_workspaces()
        if operation == "search":
            return await client.search_resource_context(search)
        return await client.read_resource_context(read)

    with pytest.raises((RuntimeError, ValidationError)) as caught:
        await invoke()
    public = str(caught.value)
    assert "native_path" not in public and "private" not in public
    assert not isinstance(caught.value, WorkspaceSessionContextAccessError)
    original = caught.value.__cause__
    assert isinstance(original, ValidationError) and original.errors()[0]["input"] == invalid
    assert all(value in public for value in (
        "Gateway 上下文查询回执协议无效", "stage=response_validation", "error_type=ValidationError",
        "method=GET" if operation == "inventory" else "method=POST",
        "path=/api/gateway/workspaces" if operation == "inventory" else "path=/api/gateway/federation/relay",
        "request_id=None" if operation == "inventory" else "request_id=req_gateway_context",
    ))
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    data = valid
    result = await invoke()
    assert all(result.model_dump()[key] == value for key, value in valid.items())
    assert wire_requests[0] == wire_requests[1]


@pytest.mark.parametrize("fault", ["json", "envelope", "request-id"])
async def test_context_protocol_failures_keep_decode_and_request_identity_boundaries(
    gateway_clients: list[GatewaySessionContextClient], monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    async def respond(_client, method, url, **kwargs):
        request = httpx.Request(method, url)
        request_id = kwargs["headers"]["X-Request-ID"]
        headers = {"X-Request-ID": request_id}
        if fault == "json":
            return httpx.Response(200, request=request, headers=headers, text="/private/context/not-json")
        return httpx.Response(200, request=request, headers=headers, json={
            "request_id": "wrong-request" if fault == "request-id" else request_id,
            "data": {} if fault == "request-id" else [],
        })

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "context-protocol-test")
    gateway_clients.append(client)
    with pytest.raises(RuntimeError) as caught:
        await client.read_resource_context(_read_request("ws_query"))
    assert not isinstance(caught.value, WorkspaceSessionContextAccessError)
    if fault == "request-id":
        assert str(caught.value) == "上下文资源 relay 响应 request_id 漂移" and caught.value.__cause__ is None
    else:
        assert "stage=response_validation" in str(caught.value) and "private" not in str(caught.value)
        assert isinstance(caught.value.__cause__, JSONDecodeError if fault == "json" else TypeError)
        if fault == "json":
            assert caught.value.__cause__.doc == "/private/context/not-json"
