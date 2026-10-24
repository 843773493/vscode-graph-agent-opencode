"""通信 startup 恢复必须等待精确源 backend，不能只看 Gateway health。"""

from __future__ import annotations

import asyncio
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
class StartupClock:
    now: float = 0

    def get_running_loop(self) -> StartupClock:
        return self

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@dataclass
class ReadyResponses:
    gateway_id: str = GATEWAY_ID
    backend_workspace_id: str = WORKSPACE_ID
    response_request_id: str = REQUEST_ID
    statuses: list[str] = field(default_factory=lambda: ["offline", "ready"])
    service_error: str = "backend 尚未监听"
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)
    clock: StartupClock = field(default_factory=StartupClock)

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
                "status": status, "services": {"workspace_api": {"status": status, "error": self.service_error}},
            }]}
        response = httpx.Response(200, request=request, json={"data": data, "request_id": self.response_request_id}, headers={"X-Request-ID": self.response_request_id})
        self.responses.append(response)
        return response


@pytest.fixture
def ready_responses(monkeypatch: pytest.MonkeyPatch) -> Iterator[ReadyResponses]:
    responses = ReadyResponses()
    monkeypatch.setattr(client_module, "asyncio", responses.clock)

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
async def test_unreachable_source_backend_fails_with_bounded_diagnostic(
    ready_responses: ReadyResponses, caplog: pytest.LogCaptureFixture,
) -> None:
    ready_responses.statuses = ["offline"]
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=0.1)
    with pytest.raises(RuntimeError, match="workspace_id=.*stage=backend_readiness, status=offline"):
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    assert any(ready_responses.responses[-1].text in record.getMessage() for record in caplog.records)


@pytest.fixture(params=["/private/startup/backend", r"C:\private\startup\backend", r"\\server\private\startup\backend"])
def native_diagnostic(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize("endpoint", ["/api/gateway/health", "/api/gateway/workspaces"])
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_startup_transport_error_keeps_classification_and_original_diagnostic(
    ready_responses: ReadyResponses, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native_diagnostic: str,
    endpoint: str, failure_type: type[httpx.RequestError],
) -> None:
    failures: list[httpx.RequestError] = []

    async def fail(client, method, path, **kwargs):
        if client.base_url.join(path).path == endpoint:
            request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
            ready_responses.requests.append(request)
            failure = failure_type("连接失败: " + native_diagnostic, request=request)
            failures.append(failure)
            raise failure
        return await ready_responses.respond(client, method, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", fail)
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    retryable = failure_type in (httpx.ConnectError, httpx.ConnectTimeout)
    with pytest.raises(RuntimeError if retryable else failure_type) as caught:
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    public = str(caught.value)
    assert native_diagnostic not in public and "private" not in public
    assert f"path={endpoint}" in public and f"request_id={REQUEST_ID}" in public
    assert f"workspace_id={WORKSPACE_ID}" in public and f"gateway_id={GATEWAY_ID}" in public
    assert f"stage=connection, error_type={failure_type.__name__}" in public
    assert caught.value.__cause__ is failures[-1]
    assert any(record.exc_info and record.exc_info[1] is failures[-1] for record in caplog.records)
    if retryable:
        assert "恢复等待超时" in public
    else:
        assert type(caught.value) is failure_type and len(failures) == 1
        assert caught.value.request is failures[0].request


@pytest.mark.parametrize("status_kind", ["offline", "invalid"])
async def test_startup_backend_diagnostic_is_logged_without_changing_readiness(
    ready_responses: ReadyResponses, caplog: pytest.LogCaptureFixture,
    native_diagnostic: str, status_kind: str,
) -> None:
    ready_responses.statuses = ["offline" if status_kind == "offline" else native_diagnostic]
    ready_responses.service_error = native_diagnostic
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    with pytest.raises(RuntimeError) as caught:
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    public = str(caught.value)
    assert native_diagnostic not in public and "private" not in public
    assert f"request_id={REQUEST_ID}" in public and caught.value.__cause__ is None
    assert any(ready_responses.responses[-1].text in record.getMessage() for record in caplog.records)
    if status_kind == "offline":
        assert "恢复等待超时" in public and "stage=backend_readiness, status=offline" in public
    else:
        assert "stage=backend_status_validation, status_type=str" in public
        assert len(ready_responses.responses) == 2


@pytest.mark.parametrize("endpoint", ["/api/gateway/health", "/api/gateway/workspaces"])
async def test_startup_http_failure_preserves_response_and_does_not_retry(
    ready_responses: ReadyResponses, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native_diagnostic: str, endpoint: str,
) -> None:
    failed_responses: list[httpx.Response] = []

    async def reject(client, method, path, **kwargs):
        if client.base_url.join(path).path != endpoint:
            return await ready_responses.respond(client, method, path, **kwargs)
        request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
        response = httpx.Response(503, request=request, text=native_diagnostic)
        failed_responses.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", reject)
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    assert len(failed_responses) == 1
    assert caught.value.response is failed_responses[0]
    assert caught.value.request is failed_responses[0].request
    original = caught.value.__cause__
    assert isinstance(original, httpx.HTTPStatusError) and original.response is caught.value.response
    public = str(caught.value)
    assert native_diagnostic not in public and "private" not in public
    assert f"path={endpoint}" in public and "stage=http_response, status=503" in public
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert any(native_diagnostic in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("endpoint", ["/api/gateway/health", "/api/gateway/workspaces"])
async def test_startup_transient_connection_failure_retries_original_target(
    ready_responses: ReadyResponses, monkeypatch: pytest.MonkeyPatch, endpoint: str,
) -> None:
    original_requests: list[httpx.Request] = []

    async def transient(client, method, path, **kwargs):
        if client.base_url.join(path).path == endpoint and not original_requests:
            request = httpx.Request(method, client.base_url.join(path), headers=client.headers)
            original_requests.append(request)
            raise httpx.ConnectError("临时连接失败", request=request)
        return await ready_responses.respond(client, method, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", transient)
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    retried = next(request for request in ready_responses.requests if request.url.path == endpoint)
    assert original_requests[0].url == retried.url and original_requests[0].headers == retried.headers
    assert ready_responses.responses[-1].json()["data"]["items"][0]["status"] == "ready"


async def test_startup_gateway_and_backend_share_one_deadline(
    ready_responses: ReadyResponses, monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets: list[float] = []
    ready_responses.statuses = ["offline"]

    async def consume_gateway_budget(client, method, path, **kwargs):
        if client.base_url.join(path).path == "/api/gateway/health":
            ready_responses.clock.now += 0.75
        else:
            budgets.append(kwargs["timeout"])
        return await ready_responses.respond(client, method, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", consume_gateway_budget)
    client = GatewayCommunicationClient(gateway_url="http://gateway-ready.test", timeout_seconds=1)
    with pytest.raises(RuntimeError, match="stage=backend_readiness, status=offline"):
        await client.wait_ready(gateway_id=GATEWAY_ID, workspace_id=WORKSPACE_ID)
    assert ready_responses.clock.now == 1 and budgets == pytest.approx([0.25, 0.15, 0.05])
