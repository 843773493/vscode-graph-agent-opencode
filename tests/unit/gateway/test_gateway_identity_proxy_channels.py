"""3.4-C：三条代理通道都必须按请求注入权威 ``X-BoxTeam-Gateway-Id``。

覆盖 Gateway 侧三条原本漏注入的代理通道：WebSocket 中继、生命周期/配置
control-plane（``_backend_headers``）、联邦冷 catalog 端口。三者取值必须与两条
HTTP 代理同源（``load_proxy_gateway_id``），且必须是本机 ``identity.json`` 里的
真实 gateway_id，而不是任何字面量或瞬时通道标识。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi import FastAPI

from app.core.path_utils import get_gateway_root
from app.gateway.auth import get_gateway_local_token
from app.gateway.auxiliary_proxy import _proxy_auxiliary_websocket
from app.gateway.federation import workspace_port as workspace_port_module
from app.gateway.federation.workspace_port import WorkspaceCatalogPort
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.workspace import WorkspaceRuntime


def _gateway_id(tmp_path: Path) -> str:
    """读取/生成本机 identity.json 的权威 gateway_id，作为断言基准。"""

    identity = get_gateway_root() / "identity.json"
    return json.loads(identity.read_text(encoding="utf-8"))["gateway_id"]


def test_backend_headers_inject_authoritative_gateway_id(tmp_path: Path) -> None:
    """生命周期/配置 control-plane 的 ``_backend_headers`` 注入真实 gateway_id。"""

    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json"),
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    headers = controller._backend_headers("req_control_plane")

    assert headers["X-BoxTeam-Gateway-Id"] == _gateway_id(tmp_path)
    assert headers["X-BoxTeam-Gateway-Id"].startswith("gateway_")
    # 既有头部语义不被改动：仍是同一 request_id，绝不补造第二个请求 ID。
    assert headers["X-Request-ID"] == "req_control_plane"
    assert headers["X-Local-Token"]


@pytest.mark.asyncio
async def test_workspace_catalog_port_injects_authoritative_gateway_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """联邦冷 catalog 端口对工作区后端的请求带真实 gateway_id。"""

    captured: list[dict[str, str]] = []
    real_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return httpx.Response(
            200,
            json={"data": {"catalog_revision": "rev_34c", "sessions": []}},
        )

    def handler_client(*, timeout: float) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(workspace_port_module.httpx, "AsyncClient", handler_client)

    port = WorkspaceCatalogPort()
    port.register_workspace(workspace_id="gw_catalog", backend_url="http://127.0.0.1:41100")

    data = await port._export(workspace_id="gw_catalog")

    assert data["catalog_revision"] == "rev_34c"
    assert len(captured) == 1
    # httpx 头部名统一小写；断言线上可见的真实权威值。
    assert captured[0]["x-boxteam-gateway-id"] == _gateway_id(tmp_path)


class _FakeWebSocket:
    """把上游握手头部回灌给测试的最小 WebSocket 替身。"""

    def __init__(self, app: object) -> None:
        self.app = app
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.accepted = False
        self._incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._incoming.put_nowait({"type": "websocket.disconnect"})

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, str]:
        return await self._incoming.get()

    async def send_text(self, message: str) -> None:  # pragma: no cover - 本用例无上游回包
        raise AssertionError(f"未预期的上游文本帧: {message!r}")

    async def send_bytes(self, message: bytes) -> None:  # pragma: no cover
        raise AssertionError(f"未预期的上游二进制帧: {message!r}")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


@pytest.mark.asyncio
async def test_auxiliary_websocket_injects_authoritative_gateway_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSocket 中继在握手时注入权威 gateway_id，客户端无法自带该头。"""

    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_ws",
            name="ws",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41100",
            connection_kind="local",
        ),
        runtime=WorkspaceRuntime(
            service_urls={"terminal_manager": "http://127.0.0.1:41101/api"}
        ),
        activate=False,
    )

    application = FastAPI()
    application.state.registry = registry
    websocket = _FakeWebSocket(application)
    websocket.query_params = {"token": get_gateway_local_token()}

    handshake: dict[str, object] = {}

    class _Upstream:
        async def send(self, message: str | bytes) -> None:
            return None

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def iterate():
                if False:  # pragma: no cover - 空上游流，仅让中继自然结束
                    yield b""
            return iterate()

        async def close(self) -> None:
            return None

    class _Connect:
        def __init__(self, url: str, **kwargs: object) -> None:
            handshake["url"] = url
            handshake["headers"] = kwargs.get("additional_headers")

        async def __aenter__(self) -> _Upstream:
            return _Upstream()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(
        "app.gateway.auxiliary_proxy.connect",
        lambda url, **kwargs: _Connect(url, **kwargs),
    )

    with anyio.move_on_after(2):
        await _proxy_auxiliary_websocket(
            websocket=websocket,  # type: ignore[arg-type]
            workspace_id="gw_ws",
            service="terminal_manager",
            socket_path="terminal",
        )

    assert websocket.accepted
    assert handshake["url"] == "ws://127.0.0.1:41101/api/terminal"
    headers = handshake["headers"]
    assert isinstance(headers, dict)
    assert headers["X-BoxTeam-Gateway-Id"] == _gateway_id(tmp_path)
    # WebSocket 握手只转发显式写入的头部；客户端自定义无关头不会漂到上游。
    assert "X-BoxTeam-Federation-Token" not in headers
    assert registry.route_reference_counts("gw_ws") == (0, 0)
