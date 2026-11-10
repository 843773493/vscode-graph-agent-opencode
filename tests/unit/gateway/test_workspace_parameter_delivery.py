"""Gateway 参数交付保留 Workspace owner 的明确冲突原因。"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest

from app.gateway.plugins.workspaces import WorkspacePluginBindings
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime
from app.schemas.gateway.service_plugin import PluginBinding

_GATEWAY_ID = "gateway_" + "a" * 32
_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_parameter_delivery_keeps_sanitized_workspace_conflict_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = "插件私有输入版本不存在: 00000000000000000000000000000000"
    request = httpx.Request("POST", "http://workspace/api/internal/service-plugins/parameters")
    response = httpx.Response(409, json={"detail": detail}, request=request)
    route = SimpleNamespace(base_url="http://workspace")
    workspace_id = _WORKSPACE_ID

    @contextmanager
    def management_routes(*, workspace_ids: tuple[str, ...], allow_starting: bool):
        assert workspace_ids == (workspace_id,)
        assert allow_starting is True
        yield (route,)

    registry_stub = SimpleNamespace(gateway_id=_GATEWAY_ID, backend_management_routes=management_routes)
    runtime_stub = SimpleNamespace(
        client=SimpleNamespace(post=AsyncMock(return_value=response)),
        declaration=lambda _binding: SimpleNamespace(contributes=SimpleNamespace(configuration={"type": "object"})),
    )
    monkeypatch.setattr("app.gateway.plugins.workspaces.get_gateway_local_token", lambda: "local-token")
    monkeypatch.setattr("app.gateway.plugins.workspaces.get_current_request_id", lambda: "request-1")
    coordinator = WorkspacePluginBindings(
        cast(GatewayWorkspaceRegistry, registry_stub), cast(GatewayPluginRuntime, runtime_stub),
    )
    binding = PluginBinding(package_id="probe.tools", plugin_id="probe", package_version="1.0.0",
                            workspace_id=workspace_id, enabled=True)

    with pytest.raises(ValueError, match="版本不存在") as caught:
        await coordinator.deliver_parameters(binding, "revision-1")

    assert str(caught.value) == detail
    runtime_stub.client.post.assert_awaited_once()
