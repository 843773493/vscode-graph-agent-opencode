from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.gateway.main import _scan_local_directories, list_local_directories


def test_scan_local_directories_only_returns_sorted_directories() -> None:
    workspace_root = (
        Path.cwd()
        / "out/tests/unit/gateway/test_directory_browser/workspace"
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "Zulu").mkdir(exist_ok=True)
    (workspace_root / "alpha").mkdir(exist_ok=True)
    (workspace_root / "README.md").write_text("not a directory", encoding="utf-8")

    entries, truncated = _scan_local_directories(workspace_root, limit=120)

    assert [entry.name for entry in entries] == ["alpha", "Zulu"]
    assert truncated is False


def test_scan_local_directories_reports_truncation() -> None:
    workspace_root = (
        Path.cwd()
        / "out/tests/unit/gateway/test_directory_browser/workspace"
    )
    entries, truncated = _scan_local_directories(workspace_root, limit=1)

    assert len(entries) == 1
    assert truncated is True


@pytest.mark.asyncio
async def test_remote_gateway_directory_browser_delegates_to_federation_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRegistry:
        def remote_gateway_url(self, connection_id: str) -> str:
            assert connection_id == "rgw_remote"
            return "http://remote-gateway.test"

    request_remote = AsyncMock(
        return_value={
            "path": "/srv/projects",
            "parent_path": "/srv",
            "home_path": "/home/remote",
            "entries": [
                {"name": "alpha project", "path": "/srv/projects/alpha project"}
            ],
            "truncated": False,
            "limit": 25,
        }
    )
    monkeypatch.setattr(
        "app.gateway.main.request_remote_gateway_management",
        request_remote,
    )
    monkeypatch.setattr(
        "app.gateway.main._remote_gateway_credential",
        lambda connection_id: f"token-for-{connection_id}",
    )

    response = await list_local_directories(
        path="/srv/projects",
        limit=25,
        gateway_connection_id="rgw_remote",
        _="local-token",
        request_id="req_remote_directory",
        registry=FakeRegistry(),  # type: ignore[arg-type]
    )

    assert response.data.path == "/srv/projects"
    assert response.data.entries[0].name == "alpha project"
    request_remote.assert_awaited_once_with(
        gateway_url="http://remote-gateway.test",
        credential="token-for-rgw_remote",
        method="GET",
        path="/api/gateway/federation/directories?path=%2Fsrv%2Fprojects&limit=25",
        request_id="req_remote_directory",
    )
