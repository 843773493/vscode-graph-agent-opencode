from __future__ import annotations

from typing import Self
from unittest.mock import Mock, patch

import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
TERMINAL_ID = "term_00000000400070008000000000000001"


@pytest.fixture
def client() -> TerminalManagerClient:
    return TerminalManagerClient(backend_url="http://terminal.test", workspace_id=WORKSPACE_ID)


def _runtime(status: str = "running") -> dict[str, object]:
    return {
        "terminal_id": TERMINAL_ID, "workspace_id": WORKSPACE_ID,
        "status": status, "transport_owner_revision": 3, "cwd_relative": "app",
    }


@pytest.mark.asyncio
async def test_create_sends_only_owner_allocated_pty_identity_and_transport_revision(
    client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def request(method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        assert (method, path) == ("POST", "/api/terminals")
        captured.update(payload)
        return {"data": _runtime()}

    monkeypatch.setattr(client, "_json_request", request)
    result = await client.create_terminal(
        terminal_id=TERMINAL_ID, owner_revision=3, title="用户终端", cwd="/workspace/app",
    )
    assert captured["workspace_id"] == WORKSPACE_ID
    assert captured["terminal_id"] == TERMINAL_ID
    assert captured["owner_revision"] == 3
    assert not {"session_id", "source_thread_id", "retention_scope", "thread_associations"} & captured.keys()
    assert result["transport_owner_revision"] == 3
    assert isinstance(result["transport_owner_revision"], int)
    assert "cwd" not in result


@pytest.mark.asyncio
async def test_delete_preserves_complete_nested_transport_receipt(
    client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        assert (method, path, payload) == ("DELETE", f"/api/terminals/{TERMINAL_ID}", None)
        return {"data": {"deleted": True, "terminal_id": TERMINAL_ID, "terminal": _runtime("deleted")}}

    monkeypatch.setattr(client, "_json_request", request)
    result = await client.delete_resource(TERMINAL_ID)
    assert result["deleted"] is True
    assert result["terminal"] == _runtime("deleted")


@pytest.mark.asyncio
async def test_list_is_workspace_transport_only_and_hides_direct_attach_url(
    client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        assert (method, path, payload) == ("GET", "/api/terminals", None)
        return {"data": [{**_runtime(), "attach_url": "http://terminal.test/direct"}]}

    monkeypatch.setattr(client, "_json_request", request)
    result = await client.list_resources()
    assert result == [_runtime()]


@pytest.mark.asyncio
async def test_revision_and_one_use_grant_consume_internal_owner_transport(
    client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object]] = []

    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        calls.append((method, path, payload))
        return {"data": {"grant_token": "single-use", "owner_revision": 4}}

    monkeypatch.setattr(client, "_json_request", request)
    await client.sync_owner_revision(TERMINAL_ID, 4)
    result = await client.issue_transport_grant(TERMINAL_ID, owner_revision=4, association="session/thread")
    assert result["grant_token"] == "single-use"
    assert calls == [
        ("POST", f"/api/terminals/{TERMINAL_ID}/owner-revision", {"owner_revision": 4}),
        ("POST", f"/api/terminals/{TERMINAL_ID}/transport-grant", {"owner_revision": 4, "association": "session/thread"}),
    ]


class _Response:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_arguments: object) -> None:
        pass

    def read(self) -> bytes:
        return b'{"data": {}}'


def test_current_config_backend_and_explicit_workspace_owner_headers() -> None:
    config = Mock(spec=ConfigService)
    urls = ["http://terminal-a", "http://terminal-b"]
    config.get_terminal_backend_url.side_effect = urls
    client = TerminalManagerClient(config_service=config, workspace_id=WORKSPACE_ID)
    config.get_terminal_backend_url.assert_not_called()
    with patch("app.services.infrastructure.terminal_manager_client.urlopen", return_value=_Response()) as send:
        client._json_request_sync("GET", "/api/terminals", None)
        client._json_request_sync("GET", "/api/terminals", None)
    assert [call.args[0].full_url for call in send.call_args_list] == [
        "http://terminal-a/api/terminals", "http://terminal-b/api/terminals",
    ]
    for call in send.call_args_list:
        headers = {name.lower(): value for name, value in call.args[0].header_items()}
        assert headers["x-boxteam-workspace-id"] == WORKSPACE_ID
        assert headers["x-boxteam-resource-owner"] == "workspace-terminal"
