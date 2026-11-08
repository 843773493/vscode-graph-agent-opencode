from __future__ import annotations

import logging
from unittest.mock import Mock

import httpx
import pytest

from app.schemas.gateway.service_plugin import PluginBinding, PluginServiceReference
from app.services.infrastructure.config import build_config_snapshot
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.resource_owners.sessions import WORKSPACE_ID

TERMINAL_ID = "term_00000000400070008000000000000001"


def test_terminal_client_rejects_another_plugin_binding() -> None:
    with pytest.raises(ValueError, match="只能消费 boxteam.developer-tools/terminal"):
        TerminalManagerClient(workspace_id=WORKSPACE_ID, binding=PluginBinding(
            package_id="another.tools", package_version="0.1.0", plugin_id="terminal",
            enabled=True, workspace_id=WORKSPACE_ID,
        ))


@pytest.fixture
async def terminal_manager_client():
    client = TerminalManagerClient(gateway_url="http://terminal.test", workspace_id=WORKSPACE_ID,
        binding=PluginBinding(package_id="boxteam.developer-tools", package_version="0.1.0",
            plugin_id="terminal", enabled=True, workspace_id=WORKSPACE_ID))
    yield client
    await client.aclose()


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
@pytest.mark.parametrize("for_stop", [False, True])
async def test_revision_and_one_use_grant_consume_internal_owner_transport(
    terminal_manager_client: TerminalManagerClient, monkeypatch: pytest.MonkeyPatch, for_stop: bool,
) -> None:
    calls: list[tuple[str, str, object]] = []

    async def request(method: str, path: str, payload: object = None, *, service=None) -> dict[str, object]:
        if service is not None:
            service.append(PluginServiceReference(gateway_id="gateway_test", package_id="boxteam.developer-tools",
                package_version="0.1.0", plugin_id="terminal", instance_id="original", service_id="socket",
                workspace_id=WORKSPACE_ID))
        calls.append((method, path, payload))
        return {"data": {"grant_token": "single-use", "owner_revision": 4}}

    monkeypatch.setattr(terminal_manager_client, "_json_request", request)
    await terminal_manager_client.sync_owner_revision(TERMINAL_ID, 4, for_stop=for_stop)
    result = await terminal_manager_client.issue_transport_grant(TERMINAL_ID, owner_revision=4, association="session/thread")
    assert result["grant_token"] == "single-use"
    assert calls == [
        ("POST", f"/api/terminals/{TERMINAL_ID}/owner-revision", {"owner_revision": 4, "for_stop": for_stop}),
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
    resource_http(lambda request: httpx.Response(200, json=response, headers={
        "X-BoxTeam-Plugin-Instance-Id": "original", "X-BoxTeam-Plugin-Gateway-Id": "gateway_test"}))
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
async def test_current_config_binding_routes_through_gateway_without_rediscovery(resource_http) -> None:
    config_service = Mock(spec=ConfigService)
    config_service.get_gateway_connection_url.return_value = "http://gateway.test"
    config_service.get_gateway_connection_timeout_seconds.return_value = 10
    def snapshot(version, enabled=True):
        return build_config_snapshot({"plugins": {"runtime": {"entries": [{
            "package_id": "boxteam.developer-tools", "plugin_id": "terminal",
            "package_version": version, "enabled": enabled}]}}}, source_paths=())
    config_service.get_snapshot.side_effect = [snapshot("0.1.0"), snapshot("0.2.0"), snapshot("0.2.0", False)]
    client = TerminalManagerClient(config_service=config_service, workspace_id=WORKSPACE_ID)
    requests: list[httpx.Request] = []
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {}})
    resource_http(respond)
    try:
        await client._json_request("GET", "/api/terminals")
        await client._json_request("GET", "/api/terminals")
        with pytest.raises(ValueError, match="已关闭"):
            await client._json_request("GET", "/api/terminals")
    finally:
        await client.aclose()
    assert len(requests) == 2
    assert [request.url.params["package_version"] for request in requests] == ["0.1.0", "0.2.0"]
    for request in requests:
        assert request.url.host == "gateway.test"
        assert request.url.path == "/api/gateway/service-plugins/boxteam.developer-tools/terminal/services/http/api/terminals"
        assert request.headers["x-boxteam-workspace-id"] == WORKSPACE_ID
        assert request.headers["x-boxteam-resource-owner"] == "workspace-terminal"
        assert request.headers["x-local-token"]
        assert request.extensions["timeout"]["read"] == 10
