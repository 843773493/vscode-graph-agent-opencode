from __future__ import annotations

import logging
from unittest.mock import Mock

import httpx
import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.resource_owners.sessions import WORKSPACE_ID

TERMINAL_ID = "term_00000000400070008000000000000001"


@pytest.fixture
def terminal_manager_client() -> TerminalManagerClient:
    return TerminalManagerClient(backend_url="http://terminal.test", workspace_id=WORKSPACE_ID)


def _terminal_record(status: str = "running") -> dict[str, object]:
    return {
        "terminal_id": TERMINAL_ID, "workspace_id": WORKSPACE_ID,
        "status": status, "transport_owner_revision": 3, "cwd_relative": "app",
    }


@pytest.mark.asyncio
async def test_create_sends_only_owner_allocated_pty_identity_and_transport_revision(
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def request(method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        assert (method, path) == ("POST", "/api/terminals")
        captured.update(payload)
        return {"data": _terminal_record()}

    monkeypatch.setattr(terminal_manager_client, "_json_request", request)
    result = await terminal_manager_client.create_terminal(
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
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        assert (method, path, payload) == ("DELETE", f"/api/terminals/{TERMINAL_ID}", None)
        return {"data": {"deleted": True, "terminal_id": TERMINAL_ID, "terminal": _terminal_record("deleted")}}

    monkeypatch.setattr(terminal_manager_client, "_json_request", request)
    result = await terminal_manager_client.delete_resource(TERMINAL_ID)
    assert result["deleted"] is True
    assert result["terminal"] == _terminal_record("deleted")


@pytest.mark.asyncio
async def test_list_is_workspace_transport_only_and_hides_direct_attach_url(
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        assert (method, path, payload) == ("GET", "/api/terminals", None)
        return {"data": [{**_terminal_record(), "attach_url": "http://terminal.test/direct"}]}

    monkeypatch.setattr(terminal_manager_client, "_json_request", request)
    result = await terminal_manager_client.list_resources()
    assert result == [_terminal_record()]


@pytest.mark.asyncio
async def test_revision_and_one_use_grant_consume_internal_owner_transport(
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object]] = []

    async def request(method: str, path: str, payload: object = None) -> dict[str, object]:
        calls.append((method, path, payload))
        return {"data": {"grant_token": "single-use", "owner_revision": 4}}

    monkeypatch.setattr(terminal_manager_client, "_json_request", request)
    await terminal_manager_client.sync_owner_revision(TERMINAL_ID, 4)
    result = await terminal_manager_client.issue_transport_grant(TERMINAL_ID, owner_revision=4, association="session/thread")
    assert result["grant_token"] == "single-use"
    assert calls == [
        ("POST", f"/api/terminals/{TERMINAL_ID}/owner-revision", {"owner_revision": 4}),
        ("POST", f"/api/terminals/{TERMINAL_ID}/transport-grant", {"owner_revision": 4, "association": "session/thread"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, data, stage", [
    ("list", "/private/terminal", "list.data"),
    ("list", ["/private/terminal"], "list.item"),
    ("get", "/private/terminal", "record.data"),
    ("read", "/private/terminal", "envelope.data"),
    ("read", {"terminal": "/private/terminal"}, "envelope.terminal"),
    ("grant", "/private/terminal", "grant.data"),
    ("get", _terminal_record("/private/terminal"), "terminal"),
])
async def test_response_protocol_failures_preserve_diagnostics_and_fail_at_the_client(
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, operation: str, data: object, stage: str, resource_http,
) -> None:
    response = {"data": data}
    resource_http(lambda request: httpx.Response(200, json=response))
    with caplog.at_level(logging.ERROR), pytest.raises(TypeError) as caught:
        if operation == "list":
            await terminal_manager_client.list_resources()
        elif operation == "read":
            await terminal_manager_client.read_terminal(TERMINAL_ID)
        elif operation == "grant":
            await terminal_manager_client.issue_transport_grant(TERMINAL_ID, owner_revision=3, association=None)
        else:
            await terminal_manager_client.get_resource(TERMINAL_ID)
    assert str(caught.value) == f"终端管理器响应协议错误: stage={stage}；请查看工作区终端服务日志"
    diagnostic = data if stage == "terminal" else response
    assert any(isinstance(record.args, tuple) and record.args[-1] == diagnostic for record in caplog.records)
    if stage == "terminal":
        assert isinstance(caught.value.__cause__, TypeError)
        assert str(caught.value.__cause__) == "Terminal 状态不在协议范围内: /private/terminal"


@pytest.mark.asyncio
async def test_current_config_backend_and_explicit_workspace_owner_headers(resource_http) -> None:
    config_service = Mock(spec=ConfigService)
    config_service.get_terminal_backend_url.side_effect = ["http://terminal-a", "http://terminal-b"]
    client = TerminalManagerClient(config_service=config_service, workspace_id=WORKSPACE_ID)
    config_service.get_terminal_backend_url.assert_not_called()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {}})

    resource_http(respond)
    await client._json_request("GET", "/api/terminals")
    await client._json_request("GET", "/api/terminals")
    assert [str(request.url) for request in requests] == [
        "http://terminal-a/api/terminals", "http://terminal-b/api/terminals",
    ]
    for request in requests:
        assert request.headers["x-boxteam-workspace-id"] == WORKSPACE_ID
        assert request.headers["x-boxteam-resource-owner"] == "workspace-terminal"
        assert request.extensions["timeout"]["read"] == 10
