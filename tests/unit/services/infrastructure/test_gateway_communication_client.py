"""通信 startup 恢复必须等待精确源 backend，不能只看 Gateway health。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.services.infrastructure import gateway_communication_client as client_module
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from tests.support.communication import WORKSPACE_ID

REQUEST_ID = "communication-startup-ready"
GATEWAY_ID = "gateway_11111111111111111111111111111111"


@dataclass
class ReadyResponses:
    gateway_id: str = GATEWAY_ID
    backend_workspace_id: str = WORKSPACE_ID
    response_request_id: str = REQUEST_ID
    statuses: list[str] = field(default_factory=lambda: ["offline", "ready"])
    requests: list[httpx.Request] = field(default_factory=list)

    async def respond(self, client: httpx.AsyncClient, method: str, path: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
        self.requests.append(request)
        assert request.headers["X-Local-Token"] == "communication-ready-token"
        assert request.headers["X-Request-ID"] == REQUEST_ID
        if request.url.path == "/api/gateway/health":
            data = {"gateway_id": self.gateway_id}
        else:
            assert request.url.path == "/api/gateway/workspaces"
            assert request.url.params["check_health"] == "true"
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            data = {"items": [{
                "connection_kind": "local", "backend_workspace_id": self.backend_workspace_id,
                "status": status, "services": {"workspace_api": {"status": status, "error": "backend 尚未监听"}},
            }]}
        return httpx.Response(200, request=request, json={"data": data, "request_id": self.response_request_id}, headers={"X-Request-ID": self.response_request_id})


@pytest.fixture
def ready_responses(monkeypatch: pytest.MonkeyPatch) -> Iterator[ReadyResponses]:
    responses = ReadyResponses()

    async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs: object) -> httpx.Response:
        return await responses.respond(client, method, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    monkeypatch.setattr(client_module, "get_gateway_local_token", lambda: "communication-ready-token")
    token = set_current_request_id(REQUEST_ID)
    try:
        yield responses
    finally:
        reset_current_request_id(token)


@pytest.mark.asyncio
async def test_ready_gateway_does_not_skip_unreachable_source_backend(ready_responses: ReadyResponses) -> None:
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    assert [request.url.path for request in ready_responses.requests] == [
        "/api/gateway/health", "/api/gateway/workspaces", "/api/gateway/workspaces",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["gateway", "backend", "request_id"])
async def test_startup_ready_rejects_identity_drift(ready_responses: ReadyResponses, drift: str) -> None:
    if drift == "gateway":
        ready_responses.gateway_id = "other-gateway"
    elif drift == "backend":
        ready_responses.backend_workspace_id = "other-workspace"
    else:
        ready_responses.response_request_id = "other-request"
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    with pytest.raises(RuntimeError, match="身份|request_id"):
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)


@pytest.mark.asyncio
async def test_unreachable_source_backend_fails_with_bounded_diagnostic(ready_responses: ReadyResponses) -> None:
    ready_responses.statuses = ["offline"]
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=0.1)
    with pytest.raises(RuntimeError, match="workspace_id=.*backend 尚未监听"):
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
