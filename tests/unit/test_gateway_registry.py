import asyncio
from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


class _HealthResponse:
    status_code = 200


class _ConcurrentHealthClient:
    active_requests = 0
    peak_requests = 0

    def __init__(self, *, timeout: int) -> None:
        assert timeout == 2

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert url.endswith("/api/v1/health")
        assert headers == {"X-Local-Token": "local-dev-token"}
        type(self).active_requests += 1
        type(self).peak_requests = max(
            type(self).peak_requests,
            type(self).active_requests,
        )
        await asyncio.sleep(0.02)
        type(self).active_requests -= 1
        return _HealthResponse()


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    for index in range(3):
        result.upsert(
            WorkspaceTarget(
                workspace_id=f"workspace-{index}",
                name=f"Workspace {index}",
                root_path=f"/tmp/workspace-{index}",
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
            ),
            activate=index == 0,
        )
    return result


@pytest.mark.asyncio
async def test_list_dtos_checks_workspace_health_concurrently(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
):
    _ConcurrentHealthClient.active_requests = 0
    _ConcurrentHealthClient.peak_requests = 0
    monkeypatch.setattr(
        "app.gateway.registry.httpx.AsyncClient",
        _ConcurrentHealthClient,
    )

    result = await registry.list_dtos()

    assert [item.workspace_id for item in result] == [
        "workspace-0",
        "workspace-1",
        "workspace-2",
    ]
    assert all(item.status == "ready" for item in result)
    assert _ConcurrentHealthClient.peak_requests == 3
