"""Backend 客户端结清原 HTTP 请求，不拥有共享管理进程。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal
from uuid import uuid4

import httpx
import pytest

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    ExtensionManagementError,
    NativeHostReference,
    NativeReferenceService,
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


@pytest.mark.parametrize("operation", ["open-host", "close-host"])
async def test_reference_cancellation_settles_same_service_without_replay(client: ExtensionManagementClient,
    operation: Literal["open-host", "close-host"],
) -> None:
    service = NativeReferenceService(url="http://127.0.0.1:45002/references",
        backend_id=client.backend_id, runtime_instance_id="original-management")
    reference = NativeHostReference(generation="original-host", capability="host-only")
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"result": reference.model_dump()})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers={"X-Local-Token": "original-runtime-token"})
    if operation == "close-host":
        client.close_admission()
    opening = asyncio.create_task(client.host_reference(service, reference, operation=operation))
    await entered.wait()
    opening.cancel("原通道已取消")
    await asyncio.sleep(0)
    assert not opening.done()
    release.set()
    with pytest.raises(asyncio.CancelledError, match="原通道已取消"):
        await opening
    assert len(requests) == 1 and str(requests[0].url) == service.url
    assert json.loads(requests[0].content) == {"method": operation, "backend_id": client.backend_id,
        "runtime_instance_id": service.runtime_instance_id, **reference.model_dump()}
    client.close_admission()
    with pytest.raises(RuntimeError, match="停止运行准入"):
        await client.host_reference(service, reference, operation="open-host")
    assert len(requests) == 1
