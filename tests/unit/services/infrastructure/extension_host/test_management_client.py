"""Backend 客户端结清原 HTTP 请求，不拥有共享管理进程。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    ExtensionManagementError,
)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ExtensionManagementClient]:
    client = ExtensionManagementClient(backend_id=str(uuid4()), gateway_url="http://127.0.0.1:45001",
        token="original-runtime-token", operation_timeout=5)
    await client._http.aclose()
    yield client
    await client.close()


async def test_client_cancel_and_close_wait_original_exchange_without_replay(client: ExtensionManagementClient) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, headers={"X-Request-ID": "original-request"}, json={
            "request_id": "original-request", "data": {"result": {"profile_id": "profile", "target_platform": "linux-x64", "extensions": []}}})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers={"X-Local-Token": "original-runtime-token"})
    token = set_current_request_id("original-request")
    try:
        request = asyncio.create_task(client.inventory())
    finally:
        reset_current_request_id(token)
    await entered.wait()
    request.cancel()
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not request.done() and not closing.done() and not client.drained
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await closing
    assert client.drained and len(requests) == 1
    assert requests[0].url.path.endswith("/" + client.backend_id)
    assert requests[0].headers["X-Local-Token"] == "original-runtime-token"
    assert requests[0].headers["X-Request-ID"] == "original-request"


@pytest.mark.parametrize("wrong", ["header", "body", "semantic-error", "lost-reply", None])
async def test_client_checks_original_receipt_and_preserves_semantic_errors(client: ExtensionManagementClient, wrong: str | None) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if wrong == "lost-reply":
            raise httpx.ReadTimeout("结果未知", request=request)
        reply = ({"error": "原 manifest 无效", "code": "Invalid"} if wrong == "semantic-error"
            else {"result": {"profile_id": "profile", "target_platform": "linux-x64", "extensions": []}})
        return httpx.Response(200, headers={"X-Request-ID": "other" if wrong == "header" else "original"}, json={
            "request_id": "other" if wrong == "body" else "original", "data": reply})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    token = set_current_request_id("original")
    try:
        if wrong in {"header", "body"}:
            with pytest.raises(ValueError, match="原请求"):
                await client.inventory()
        elif wrong == "semantic-error":
            with pytest.raises(ExtensionManagementError, match="原 manifest 无效") as error:
                await client.inventory()
            assert error.value.code == "Invalid"
        elif wrong == "lost-reply":
            with pytest.raises(httpx.ReadTimeout, match="结果未知"):
                await client.inventory()
        else:
            assert (await client.inventory()).profile_id == "profile"
    finally:
        reset_current_request_id(token)
    assert calls == 1
