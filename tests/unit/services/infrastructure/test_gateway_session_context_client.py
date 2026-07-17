from __future__ import annotations

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
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
        await client.recent_text_in_workspace(
            "gw_missing_backend",
            "ses_target",
        )

    message = str(captured.value)
    assert "无法连接 Workspace Gateway" in message
    assert "gateway_url=http://127.0.0.1:65530" in message
    assert "workspace_id=gw_missing_backend" in message
    assert "path=/api/v1/sessions/ses_target/context/recent-text" in message
    assert "error_type=ConnectError" in message
    assert isinstance(captured.value.__cause__, httpx.ConnectError)


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
        await client.recent_text_in_workspace("gw_typo", "ses_target")

    message = str(captured.value)
    assert "workspace_id=gw_typo" in message
    assert "status=404" in message
    assert "修正 workspace_id 或 session_id 后重试" in message
    assert "无法确认正确标识时请提醒用户" in message


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
        await client.recent_text_in_workspace("gw_valid", "ses_target")

    assert not isinstance(captured.value, WorkspaceSessionContextAccessError)
