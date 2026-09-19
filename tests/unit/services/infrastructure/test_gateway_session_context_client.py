from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R18 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例全部针对 127.0.0.1 假端点构造 httpx 客户端（request 均被
    monkeypatch 替换），自身不经任何网络；而环境的 NO_PROXY 含 IPv6 字
    面量（``::1``/``[::1]``）会让 httpx 客户端构造时的代理解析直接抛
    ``InvalidURL: Invalid port: ':1]'``——属外部环境噪声混入测试。与
    R17 的 /tmp→tmp_path 同类测试卫生修复（任务书 §2.2 许可的 monkeypatch
    清 ``*_proxy`` 做法）；清掉后客户端不取代理，与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _read_request(workspace_id: str, session_id: str = "ses_target"):
    return SessionContextReadRequest(
        resource=f"boxteam://workspace/{workspace_id}/session/{session_id}"
    )


@pytest.mark.asyncio
async def test_gateway_connection_error_includes_target_context(
    monkeypatch: pytest.MonkeyPatch,
):
    async def raise_connect_error(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, f"http://127.0.0.1:65530{path}")
        raise httpx.ConnectError("All connection attempts failed", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "request", raise_connect_error)
    client = GatewaySessionContextClient(
        gateway_url="http://127.0.0.1:65530",
    )

    with pytest.raises(WorkspaceSessionContextAccessError) as captured:
        await client.read_context_in_workspace(
            "gw_missing_backend",
            _read_request("gw_missing_backend"),
        )

    message = str(captured.value)
    assert "无法连接 Workspace Gateway" in message
    assert "workspace_id=gw_missing_backend" in message
    assert "path=/api/v1/context/read" in message
    assert "error_type=ConnectError" in message
    assert isinstance(captured.value.__cause__, httpx.ConnectError)


@pytest.mark.asyncio
async def test_gateway_client_uses_default_runtime_url_without_local_token(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_request: httpx.Request | None = None

    async def capture_request(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        nonlocal captured_request
        captured_request = httpx.Request(
            method,
            f"http://127.0.0.1:8014{path}",
            headers=_client.headers,
        )
        return httpx.Response(
            404,
            request=captured_request,
            json={"detail": "Gateway 工作区不存在: gw_target"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", capture_request)
    client = GatewaySessionContextClient()

    with pytest.raises(WorkspaceSessionContextAccessError):
        await client.read_context_in_workspace(
            "gw_target",
            _read_request("gw_target"),
        )

    assert captured_request is not None
    assert captured_request.url.host == "127.0.0.1"
    assert captured_request.url.port == 8014
    assert captured_request.headers["X-BoxTeam-Workspace-Id"] == "gw_target"
    assert "X-Local-Token" not in captured_request.headers


@pytest.mark.asyncio
async def test_gateway_unknown_workspace_is_model_recoverable(
    monkeypatch: pytest.MonkeyPatch,
):
    async def return_not_found(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, f"http://127.0.0.1:8014{path}")
        return httpx.Response(
            404,
            request=request,
            json={"detail": "Gateway 工作区不存在: gw_typo"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", return_not_found)
    client = GatewaySessionContextClient(gateway_url="http://127.0.0.1:8014")

    with pytest.raises(WorkspaceSessionContextAccessError) as captured:
        await client.read_context_in_workspace(
            "gw_typo",
            _read_request("gw_typo"),
        )

    message = str(captured.value)
    assert "workspace_id=gw_typo" in message
    assert "status=404" in message
    assert "Gateway 工作区不存在" in message


@pytest.mark.asyncio
async def test_gateway_internal_server_error_still_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
):
    async def return_server_error(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, f"http://127.0.0.1:8014{path}")
        return httpx.Response(500, request=request, text="internal invariant broken")

    monkeypatch.setattr(httpx.AsyncClient, "request", return_server_error)
    client = GatewaySessionContextClient(gateway_url="http://127.0.0.1:8014")

    with pytest.raises(RuntimeError, match="status=500") as captured:
        await client.read_context_in_workspace(
            "gw_valid",
            _read_request("gw_valid"),
        )

    assert not isinstance(captured.value, WorkspaceSessionContextAccessError)


@pytest.mark.asyncio
async def test_gateway_url_is_resolved_from_current_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_service = Mock(spec=ConfigService)
    current_url = ["http://gateway-a"]
    config_service.get_gateway_connection_url.side_effect = lambda: current_url[0]
    config_service.get_gateway_connection_timeout_seconds.return_value = 30
    captured_urls: list[str] = []

    async def capture_request(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        captured_urls.append(f"{client.base_url}{path}")
        request = httpx.Request(method, f"{client.base_url}{path}")
        return httpx.Response(
            200,
            request=request,
            json={
                "data": {
                    "resource": "boxteam://workspace/gw_target/session/ses_target",
                    "view": "overview",
                    "revision": "rev-1",
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", capture_request)
    client = GatewaySessionContextClient(config_service=config_service)

    await client.read_context_in_workspace(
        "gw_target",
        _read_request("gw_target"),
    )
    current_url[0] = "http://gateway-b"
    await client.read_context_in_workspace(
        "gw_target",
        _read_request("gw_target"),
    )

    assert captured_urls == [
        "http://gateway-a/api/v1/context/read",
        "http://gateway-b/api/v1/context/read",
    ]
