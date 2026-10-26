"""管理调用必须确认实际进程与设置，不能把 HTTP 成功当作已应用。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from app.core.logging_config import RuntimeLoggingConflictError
from app.gateway.runtime.logging_control import RuntimeLoggingProcessControl
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingHttpService,
)


@pytest.fixture
def logging_state() -> dict[str, object]:
    return {
        "service": "backend", "pid": 43210, "runtime_instance_id": "instance-a",
        "applied_revision": None, "level": "info", "pretty": True, "log_sink": None,
    }


@pytest.fixture
def logging_request() -> RuntimeLoggingApply:
    return RuntimeLoggingApply(
        runtime_instance_id="instance-a", revision="revision-b", expected_revision=None,
        level="DEBUG", pretty=False,
    )


@pytest.fixture
def control_client():
    def create(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2, trust_env=False)
    return create


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "path"), [
    ("gateway", "/api/gateway/internal/runtime-logging"),
    ("backend", "/api/v1/internal/runtime-logging"),
    ("terminal", "/internal/runtime-logging"),
    ("browser", "/internal/runtime-logging"),
])
async def test_status_uses_private_process_channel(
    control_client, logging_state: dict[str, object], service: RuntimeLoggingHttpService, path: str,
) -> None:
    logging_state["service"] = service

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == path
        assert request.headers["X-Local-Token"] == "private-control-token"
        assert "X-BoxTeam-Workspace-Id" not in request.headers
        return httpx.Response(200, json={"data": logging_state, "request_id": "request-a"})

    async with control_client(respond) as client:
        state = await RuntimeLoggingProcessControl(client=client, token="private-control-token").status(
            base_url="http://127.0.0.1:43000/", service=service, pid=43210,
        )
    assert state.model_dump(mode="json") == logging_state


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://127.0.0.1:43000", "http://example.com:43000", "http://127.0.0.1",
    "http://127.0.0.1:43000/api", "http://user:secret@127.0.0.1:43000",
    "http://127.0.0.1:43000?token=secret", "http://127.0.0.1:43000#fragment",
])
async def test_control_rejects_nonlocal_or_ambiguous_targets(control_client, url: str) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("非法控制地址不能发出请求")

    async with control_client(unexpected) as client:
        with pytest.raises(ValueError, match="本机 HTTP 服务根地址"):
            await RuntimeLoggingProcessControl(client=client, token="private-control-token").status(
                base_url=url, service="backend", pid=43210,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"service": "terminal"}, {"pid": 43211}])
async def test_status_rejects_different_process(control_client, logging_state, changes) -> None:
    async with control_client(lambda _: httpx.Response(200, json={"data": logging_state | changes})) as client:
        with pytest.raises(RuntimeLoggingConflictError, match="身份"):
            await RuntimeLoggingProcessControl(client=client, token="private-control-token").status(
                base_url="http://localhost:43000", service="backend", pid=43210,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {}, {"data": {"service": "backend", "pid": 43210}},
    {"data": {"service": "backend", "pid": "43210"}},
])
async def test_status_rejects_incomplete_proof(control_client, body) -> None:
    async with control_client(lambda _: httpx.Response(200, json=body)) as client:
        with pytest.raises((ValueError, ValidationError)):
            await RuntimeLoggingProcessControl(client=client, token="private-control-token").status(
                base_url="http://[::1]:43000", service="backend", pid=43210,
            )


@pytest.mark.asyncio
async def test_control_does_not_follow_redirect_with_private_credentials(control_client) -> None:
    requests = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://example.com/control"})

    async with control_client(redirect) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await RuntimeLoggingProcessControl(client=client, token="private-control-token").status(
                base_url="http://127.0.0.1:43000", service="backend", pid=43210,
            )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {}, {"runtime_instance_id": "old-instance"}, {"applied_revision": "old-revision"},
    {"level": "info"}, {"pretty": True},
])
async def test_apply_requires_complete_actual_confirmation(
    control_client, logging_state, logging_request: RuntimeLoggingApply, changes,
) -> None:
    expected = logging_state | {"applied_revision": "revision-b", "level": "debug", "pretty": False}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.headers["X-Local-Token"] == "private-control-token"
        assert json.loads(request.content) == logging_request.model_dump(mode="json")
        return httpx.Response(200, json={"data": expected | changes})

    async with control_client(respond) as client:
        control = RuntimeLoggingProcessControl(client=client, token="private-control-token")
        if changes:
            with pytest.raises(RuntimeLoggingConflictError, match="未确认"):
                await control.apply(base_url="http://127.0.0.1:43000", service="backend", pid=43210, request=logging_request)
        else:
            state = await control.apply(base_url="http://127.0.0.1:43000", service="backend", pid=43210, request=logging_request)
            assert state.model_dump(mode="json") == expected
