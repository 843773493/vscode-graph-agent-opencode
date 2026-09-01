from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import WorkspaceTarget
from app.schemas.internal_v2.session import SessionDTO


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"data": {"session_id": "ses_live", "items": []}}


class _HttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return _Response()


@pytest.mark.asyncio
async def test_resource_catalog_uses_fast_workspace_resource_projection() -> None:
    http_client = _HttpClient()
    service = GatewayResourceCatalogService(
        registry=object(),  # type: ignore[arg-type]
        http_client=http_client,  # type: ignore[arg-type]
    )
    target = WorkspaceTarget(
        workspace_id="gw_test",
        name="测试工作区",
        root_path="/tmp/test-workspace",
        backend_url="http://127.0.0.1:8010",
        connection_kind="local",
        managed=True,
    )
    session = SessionDTO(
        session_id="ses_live",
        workspace_id="gw_test",
        title="运行中会话",
        current_agent_id="agent_test",
        created_at=datetime(2026, 7, 5, 1, 2, 3, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, 1, 2, 4, tzinfo=UTC),
    )

    resources = await service._load_session_resources(
        target,
        session,
        request_id="req_resources",
    )

    assert resources == []
    assert len(http_client.calls) == 1
    _url, kwargs = http_client.calls[0]
    assert kwargs["params"] == {"include_history": "false"}
