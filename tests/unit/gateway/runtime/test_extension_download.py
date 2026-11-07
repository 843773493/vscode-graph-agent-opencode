"""目标下载保留原状态、压缩流和代理归属；完整安装另由真实集成验证。"""

from __future__ import annotations

import gzip
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from app.gateway.runtime import extension_download as downloads


class AssetStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body, self.closed = body, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body[:3]
        yield self.body[3:]

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class DownloadNetwork:
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    options: list[dict[str, object]] = field(default_factory=list)
    replies: list[httpx.Response] = field(default_factory=list)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> DownloadNetwork:
    case = DownloadNetwork()
    native_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        case.requests.append(request)
        return case.replies.pop(0)

    def client(**options: object) -> httpx.AsyncClient:
        case.options.append(options)
        timeout = options["timeout"]
        assert timeout is None or isinstance(timeout, float)
        http = native_client(transport=httpx.MockTransport(respond), trust_env=False,
            timeout=timeout)
        case.clients.append(http)
        return http

    monkeypatch.setattr(downloads.httpx, "AsyncClient", client)
    return case


@pytest.fixture
def payload() -> downloads.ExtensionDownloadRequest:
    return downloads.ExtensionDownloadRequest.model_validate({
        "call_site": "extensionGalleryService.download", "url": "https://assets.example/probe.vsix?install=true",
        "strict_ssl": True,
        "headers": {"X-Asset-Token": "origin-only", "X-Local-Token": "must-not-leak", "X-Request-ID": "local-request",
            "Connection": "X-Hop", "X-Hop": "must-not-leak", "Accept": ["application/vsix", "*/*"]},
    })


@pytest.fixture
def http_scope() -> Scope:
    return {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST", "path": "/download",
        "headers": [], "query_string": b"", "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 9000),
        "scheme": "http", "root_path": "", "http_version": "1.1"}


async def test_download_preserves_raw_compressed_body_status_and_headers(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest, http_scope: Scope,
) -> None:
    compressed = gzip.compress(b"target-package-content")
    body = AssetStream(compressed)
    network.replies.append(httpx.Response(206, headers=[("Content-Encoding", "gzip"),
        ("Content-Length", str(len(compressed))), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
        ("Connection", "X-Hop"), ("X-Hop", "not-for-node"),
        (downloads.DOWNLOAD_ERROR_HEADER, "not-an-internal-error")], stream=body))
    response = await downloads.extension_download(payload)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(http_scope, receive, send)
    assert response.status_code == 206
    assert b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body") == compressed
    assert response.headers["Content-Length"] == str(len(compressed)) and response.headers["Content-Encoding"] == "gzip"
    assert response.headers.getlist("set-cookie") == ["a=1", "b=2"]
    assert "x-hop" not in response.headers and downloads.DOWNLOAD_ERROR_HEADER not in response.headers
    [request] = network.requests
    assert request.headers["X-Asset-Token"] == "origin-only"
    assert all(name not in request.headers for name in ("X-Local-Token", "X-Request-ID", "X-Hop"))
    assert request.headers.get("Connection") != "X-Hop"
    assert "Accept-Encoding" not in request.headers
    assert body.closed and all(client.is_closed for client in network.clients)
    assert network.options[0]["trust_env"] is False


@pytest.mark.parametrize("budget,expected_status,expected_requests", [(0, 302, 1), (1, 307, 2), (2, 200, 3)])
async def test_redirect_budget_returns_original_final_status_without_another_retry(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest,
    http_scope: Scope, budget: int, expected_status: int, expected_requests: int,
) -> None:
    streams = [AssetStream(b"first"), AssetStream(b"second"), AssetStream(b"package")]
    network.replies.extend([httpx.Response(302, headers={"Location": "/second"}, stream=streams[0]),
        httpx.Response(307, headers={"Location": "/third"}, stream=streams[1]), httpx.Response(200, stream=streams[2])])
    response = await downloads.extension_download(payload.model_copy(update={"follow_redirects": budget}))

    async def send(_message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(http_scope, receive, send)
    assert response.status_code == expected_status and len(network.requests) == expected_requests
    assert all(stream.closed for stream in streams[:expected_requests])
    assert all(client.is_closed for client in network.clients)


@pytest.mark.parametrize("location", ["https://assets.example/next", "https://cdn.example/next"])
async def test_redirect_preserves_original_node_headers_and_never_uses_response_cookie(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest, http_scope: Scope, location: str,
) -> None:
    network.replies.extend([httpx.Response(302, headers={"Location": location,
        "Set-Cookie": "server_session=replacement; Path=/"}, stream=AssetStream(b"redirect")),
        httpx.Response(200, stream=AssetStream(b"package"))])
    original = payload.model_copy(update={"headers": {"Authorization": "Bearer gallery-token", "Cookie": "required_session=original"}})
    response = await downloads.extension_download(original)

    async def send(_message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(http_scope, receive, send)
    assert len(network.requests) == 2
    for request in network.requests:
        assert request.headers["Authorization"] == "Bearer gallery-token"
        assert request.headers["Cookie"] == "required_session=original"


async def test_http_proxy_constructs_real_client_without_tls_context_for_plain_proxy(
    payload: downloads.ExtensionDownloadRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stop_at_network(_self: httpx.AsyncClient, _request: httpx.Request, *, stream: bool) -> httpx.Response:
        assert stream
        raise httpx.ConnectError("network-boundary-reached")

    monkeypatch.setattr(httpx.AsyncClient, "send", stop_at_network)
    configured = downloads.ExtensionDownloadRequest.model_validate({**payload.model_dump(mode="json"),
        "proxy_url": "http://127.0.0.1:9"})
    with pytest.raises(httpx.ConnectError, match="network-boundary-reached"):
        await downloads.extension_download(configured)


async def test_disconnect_before_first_body_iteration_closes_download(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest, http_scope: Scope,
) -> None:
    body = AssetStream(b"not-consumed")
    network.replies.append(httpx.Response(200, stream=body))
    response = await downloads.extension_download(payload)

    async def send(_message: Message) -> None:
        raise OSError("Node已断连")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response(http_scope, receive, send)
    assert body.closed and all(client.is_closed for client in network.clients)


async def test_proxy_authorization_never_reaches_origin(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest, http_scope: Scope,
) -> None:
    network.replies.append(httpx.Response(200, stream=AssetStream(b"proxy-package")))
    configured = downloads.ExtensionDownloadRequest.model_validate({**payload.model_dump(mode="json"),
        "proxy_url": "http://proxy.example:8080", "headers": {"Proxy-Authorization": "Basic proxy-secret"}})
    response = await downloads.extension_download(configured)

    async def send(_message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(http_scope, receive, send)
    proxy = network.options[0]["proxy"]
    assert isinstance(proxy, httpx.Proxy) and proxy.headers["Proxy-Authorization"] == "Basic proxy-secret"
    assert "Proxy-Authorization" not in network.requests[0].headers
    with pytest.raises(ValueError, match="必须绑定唯一"):
        await downloads.extension_download(configured.model_copy(update={"proxy_url": None}))


async def test_download_logs_keep_status_without_signed_url_or_wire_secrets_and_restore_other_requests(
    network: DownloadNetwork, payload: downloads.ExtensionDownloadRequest, http_scope: Scope,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class WireStream(AssetStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            logging.getLogger("httpcore.http11").debug("receive_response_headers.complete token=wire-secret")
            yield self.body

        async def aclose(self) -> None:
            logging.getLogger("httpcore.connection").debug("close.complete proxy=proxy-secret")
            await super().aclose()

    caplog.set_level(logging.DEBUG)
    network.replies.append(httpx.Response(200, stream=WireStream(b"package")))
    response = await downloads.extension_download(payload)

    async def send(_message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    # 其它请求不能因一个下载在途而失去原有日志。
    logging.getLogger("httpx").info("unrelated-request-still-visible")
    await response(http_scope, receive, send)
    logging.getLogger("httpcore.connection").debug("unrelated-connection-still-visible")
    assert "host=assets.example status=200" in caplog.text
    assert "unrelated-request-still-visible" in caplog.text and "unrelated-connection-still-visible" in caplog.text
    assert all(secret not in caplog.text for secret in ("install=true", "wire-secret", "proxy-secret", "origin-only"))
