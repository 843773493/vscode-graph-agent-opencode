"""具名 Provider 边界单测：原处理函数、ASGI 状态和日志，不冒充真实网络验收。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.types import Message, Scope
from starlette.websockets import WebSocket

from app.api import extension_host as backend
from app.container import WorkspaceContainerRegistry
from app.gateway.auth import GatewayAuthContext
from app.gateway.server import extension_host as gateway
from app.schemas.gateway.mcp_definitions import (
    ExtensionMcpDefinitionChannelDTO,
    ExtensionMcpDefinitionChannelRequest,
    McpProviderFrameError,
)
from app.services.infrastructure.extension_host.mcp_definitions import (
    McpDefinitionChannel,
)

PRIVATE = "provider-frame-private-token"
REQUEST_ID = "original-provider-request"
CLIENT_ID = "original-provider-client"


@pytest.fixture
def binding() -> ExtensionMcpDefinitionChannelRequest:
    return ExtensionMcpDefinitionChannelRequest(protocol_version=1, gateway_id="gateway_" + "a" * 32,
        backend_id="00000000-0000-4000-8000-000000000001", view_id="original-view",
        workspace_ids=[], generation="original-generation", host_kind="node")


@pytest.fixture(params=["unknown-kind", "secret-kind", "truncated", "header-value", "header-key", "extra-key"])
def malformed_frame(request: pytest.FixtureRequest) -> tuple[str, str]:
    frame = {"kind": "resolved", "request_id": "resolve-original", "launch": {
        "transport": "streamable_http", "url": "https://example.com/mcp", "headers": {"Authorization": PRIVATE}}}
    if request.param in {"unknown-kind", "secret-kind"}:
        frame["kind"] = PRIVATE if request.param == "secret-kind" else "unknown"
        return json.dumps(frame), "union_tag_invalid"
    if request.param == "truncated":
        return json.dumps(frame)[:-1], "json_invalid"
    launch = cast(dict[str, object], frame["launch"])
    if request.param == "extra-key":
        frame[PRIVATE] = PRIVATE
        return json.dumps(frame), "extra_forbidden"
    launch["headers"] = {PRIVATE if request.param == "header-key" else "Authorization": {"value": PRIVATE}}
    return json.dumps(frame), "string_type"


@dataclass
class SocketCase:
    socket: WebSocket
    sent: list[Message]


@pytest.fixture
def socket_factory(binding: ExtensionMcpDefinitionChannelRequest) -> Callable[..., SocketCase]:
    def create(raw: str, *, invalid_handshake: bool = False) -> SocketCase:
        incoming: asyncio.Queue[Message] = asyncio.Queue()
        for message in ({"type": "websocket.connect"},
                *(() if invalid_handshake else ({"type": "websocket.receive", "text": binding.model_dump_json()},)),
                {"type": "websocket.receive", "text": raw}):
            incoming.put_nowait(message)
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {"type": "websocket", "path": "/provider", "query_string": b"",
            "headers": [(b"x-boxteam-extension-client-id", CLIENT_ID.encode()),
                (b"x-boxteam-gateway-id", binding.gateway_id.encode())],
            "state": {"request_id": REQUEST_ID}, "app": SimpleNamespace(state=SimpleNamespace(
                registry=SimpleNamespace(gateway_id=binding.gateway_id)))}
        return SocketCase(WebSocket(scope, incoming.get, send), sent)

    return create


@dataclass
class BackendHosts:
    channel: McpDefinitionChannel = field(default_factory=McpDefinitionChannel)

    @asynccontextmanager
    async def mcp_definition_channel(self, _payload: ExtensionMcpDefinitionChannelRequest, *,
            client_id: str) -> AsyncIterator[tuple[McpDefinitionChannel, asyncio.Event]]:
        assert client_id == CLIENT_ID
        yield self.channel, asyncio.Event()


@pytest.fixture
def backend_registry() -> WorkspaceContainerRegistry:
    return cast(WorkspaceContainerRegistry, SimpleNamespace(extension_hosts=BackendHosts()))


@dataclass
class Upstream:
    binding: ExtensionMcpDefinitionChannelRequest
    sent: list[str] = field(default_factory=list)
    bound: bool = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self.bound:
            self.bound = True
            return ExtensionMcpDefinitionChannelDTO(**self.binding.model_dump(),
                client_id=CLIENT_ID, request_id=REQUEST_ID).model_dump_json()
        await asyncio.Event().wait()
        raise AssertionError("测试只在握手后等待原入站失败")


@pytest.fixture
def gateway_upstream(monkeypatch: pytest.MonkeyPatch, binding: ExtensionMcpDefinitionChannelRequest) -> Upstream:
    upstream = Upstream(binding)
    route = SimpleNamespace(remote=False, base_url="http://127.0.0.1:1", headers={}, leases=())

    @contextmanager
    def installation_route(*_args: object, **_kwargs: object) -> Iterator[SimpleNamespace]:
        yield route

    @asynccontextmanager
    async def connect(*_args: object, **_kwargs: object) -> AsyncIterator[Upstream]:
        yield upstream

    async def watch(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    def check(*_args: object, **_kwargs: object) -> None:
        return

    monkeypatch.setattr(gateway, "_access", lambda _socket: (GatewayAuthContext(kind="local"), None, CLIENT_ID))
    monkeypatch.setattr(gateway, "_installation_route", installation_route)
    monkeypatch.setattr(gateway, "connect", connect)
    monkeypatch.setattr(gateway, "_check", check)
    monkeypatch.setattr(gateway, "check_management_route", check)
    monkeypatch.setattr(gateway, "_watch_policy", watch)
    monkeypatch.setattr(gateway, "_watch_management_route", watch)
    return upstream


def _assert_failure(case: SocketCase, caplog: pytest.LogCaptureFixture,
        binding: ExtensionMcpDefinitionChannelRequest, error_code: str, layer: str) -> None:
    assert case.sent[-1] == {"type": "websocket.close", "code": 1011, "reason": "mcp_provider_channel_failed"}
    assert case.sent[1]["type"] == "websocket.send"
    assert json.loads(case.sent[1]["text"])["request_id"] == REQUEST_ID
    assert PRIVATE not in caplog.text
    assert all(value in caplog.text for value in (f"{layer} Provider 控制通道失败", REQUEST_ID,
        binding.gateway_id, binding.backend_id, binding.generation, "McpProviderFrameError", "ValidationError", error_code, "loc="))


async def test_backend_frame_failure_keeps_original_owner_and_safe_log(socket_factory, malformed_frame,
        backend_registry, binding, caplog) -> None:
    raw, code = malformed_frame
    case = socket_factory(raw)
    with caplog.at_level(logging.ERROR, logger=backend.__name__), pytest.raises(McpProviderFrameError) as failure:
        await backend.extension_mcp_definitions(case.socket, backend_registry)
    assert PRIVATE not in str(failure.value)
    _assert_failure(case, caplog, binding, code, "Backend")


async def test_gateway_frame_failure_keeps_original_owner_and_safe_log(socket_factory, malformed_frame,
        gateway_upstream, binding, caplog) -> None:
    raw, code = malformed_frame
    case = socket_factory(raw)
    with caplog.at_level(logging.ERROR, logger=gateway.__name__):
        await gateway.extension_mcp_definitions(case.socket)
    _assert_failure(case, caplog, binding, code, "Gateway")
    assert len(gateway_upstream.sent) == 1, "坏帧不能透传给 Backend"


@pytest.mark.parametrize("layer", ["Backend", "Gateway"])
async def test_invalid_handshake_key_never_enters_named_channel_log(socket_factory, backend_registry,
        gateway_upstream, binding, caplog, layer) -> None:
    raw = json.dumps({**binding.model_dump(), PRIVATE: PRIVATE})
    case = socket_factory(raw, invalid_handshake=True)
    with caplog.at_level(logging.ERROR):
        if layer == "Backend":
            with pytest.raises(McpProviderFrameError):
                await backend.extension_mcp_definitions(case.socket, backend_registry)
        else:
            await gateway.extension_mcp_definitions(case.socket)
    assert case.sent[-1] == {"type": "websocket.close", "code": 1011, "reason": "mcp_provider_channel_failed"}
    assert PRIVATE not in caplog.text
    assert all(value in caplog.text for value in (layer, REQUEST_ID, "ValidationError", "extra_forbidden", "<input-key>"))
    assert not gateway_upstream.sent


async def test_upstream_binding_failure_keeps_original_owner_without_input_key(socket_factory, gateway_upstream,
        binding, caplog, monkeypatch) -> None:
    async def receive() -> str:
        return json.dumps({**binding.model_dump(), "client_id": CLIENT_ID, "request_id": REQUEST_ID, PRIVATE: PRIVATE})

    monkeypatch.setattr(gateway_upstream, "recv", receive)
    case = socket_factory('{"kind":"invalidate","provider_ref":"unused"}')
    with caplog.at_level(logging.ERROR, logger=gateway.__name__):
        await gateway.extension_mcp_definitions(case.socket)
    assert case.sent[-1] == {"type": "websocket.close", "code": 1011, "reason": "mcp_provider_channel_failed"}
    assert len(case.sent) == 2, "校验失败的上游 binding 不能披露给原客户端"
    assert PRIVATE not in caplog.text
    assert all(value in caplog.text for value in (REQUEST_ID, binding.gateway_id, binding.backend_id,
        binding.generation, "ValidationError", "extra_forbidden", "<input-key>"))
