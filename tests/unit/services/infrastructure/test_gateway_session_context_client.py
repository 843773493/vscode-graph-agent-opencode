from __future__ import annotations

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
)


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
