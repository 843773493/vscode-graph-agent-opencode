"""远端日志沿原管理传输；替身只位于发送边界，不复制正文查询。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from time import time

import httpx
import pytest

from app.core.path_utils import get_gateway_root
from app.gateway.control.diagnostic_log_transport import read_diagnostic_log_target
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.log_query import RuntimeLogQueryError
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.diagnostics import GatewayDiagnosticLogQueryRequest

REMOTE = "gateway_" + "b" * 32
WORKSPACE = "00000000-0000-4000-8000-000000000001"


class HeldBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def remote_logs(diagnostic_registry: GatewayWorkspaceRegistry) -> tuple[GatewayWorkspaceRegistry, GatewayDiagnosticLogQueryRequest]:
    registry = diagnostic_registry
    connection = RemoteGatewayConnection(connection_id="logs-remote", name="远端日志", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=REMOTE, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual")
    registry.apply_remote_projection_snapshot(connection=connection, projections=(WorkspaceTarget(
        gateway_id=REMOTE, workspace_id=WORKSPACE, name="远端 A", root_path=None, backend_url="", connection_kind="remote_gateway",
        owner="remote_projection", target_namespace="gateway-remote", remote_gateway_connection_ids=(connection.connection_id,),
        remote_service_names=("workspace_api",),
    ),), runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id=connection.connection_id, peer_gateway_id=registry.gateway_id)
    return registry, GatewayDiagnosticLogQueryRequest(scope={"source": "workspace", "workspace_ids": [WORKSPACE]}, query="😀词")


@pytest.mark.parametrize("mode", ["success", "header", "envelope", "owner", "query", "large", "owner_error"])
async def test_original_remote_management_contract_and_close(remote_logs, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    registry, payload = remote_logs
    calls, releases, responses = [], [], []
    original = registry.release_remote_gateway_reference

    def release(connection: str) -> None:
        releases.append(connection)
        original(connection)

    monkeypatch.setattr(registry, "release_remote_gateway_reference", release)

    async def send(**kwargs):
        calls.append(kwargs)
        headers = {"X-Request-ID": "other" if mode == "header" else "req_remote_logs"}
        data = {"gateway_id": "gateway_" + "c" * 32 if mode == "owner" else REMOTE,
                "scope": payload.scope.model_dump(mode="json"), "query": "other" if mode == "query" else payload.query,
                "items": [], "sources": [], "complete": True, "scanned_sources": 0, "scanned_lines": 0, "scanned_bytes": 0}
        response = httpx.Response(409 if mode == "owner_error" else 200, headers=headers,
            request=httpx.Request("POST", "http://remote.test/api/gateway/federation/diagnostics/query"),
            json={"detail": {"owner": "runtime_log", "code": "stale_runtime_log_cursor", "reason": "file_changed", "message": "原文件变化"}}
            if mode == "owner_error" else {"code": 0, "request_id": "other" if mode == "envelope" else "req_remote_logs", "data": data})
        responses.append(response)
        return response

    monkeypatch.setattr("app.gateway.control.diagnostic_log_transport.send_remote_gateway_management", send)
    if mode == "success":
        result = await read_diagnostic_log_target(registry, payload, gateway_id=REMOTE, request_id="req_remote_logs")
        assert result.complete and result.gateway_id == REMOTE
    else:
        with pytest.raises(RuntimeLogQueryError) as caught:
            await read_diagnostic_log_target(registry, payload, gateway_id=REMOTE, request_id="req_remote_logs", max_bytes=32 if mode == "large" else 8192)
        error = caught.value
        assert error.code == ("delivery_limit" if mode == "large" else "stale_runtime_log_cursor" if mode == "owner_error" else "invalid_response")
        if mode == "owner_error":
            assert error.reason == "file_changed" and error.http_status == 409 and str(error) == "原文件变化"
    assert len(calls) == 1 and calls[0]["path"] == "/api/gateway/federation/diagnostics/query"
    assert calls[0]["request_id"] == "req_remote_logs" and calls[0]["stream"]
    assert calls[0]["credential"].token and calls[0]["payload"] == payload.model_dump(mode="json")
    assert releases == ["logs-remote"] and all(response.is_closed for response in responses)


@pytest.mark.parametrize("cancel", [True, False])
async def test_remote_body_cancel_and_total_deadline_release_reference(remote_logs, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    registry, payload = remote_logs
    body = HeldBody()
    released = asyncio.Event()
    original = registry.release_remote_gateway_reference

    def release(connection: str) -> None:
        original(connection)
        released.set()

    monkeypatch.setattr(registry, "release_remote_gateway_reference", release)

    async def send(**kwargs):
        return httpx.Response(200, headers={"X-Request-ID": "req_remote_logs"}, stream=body)

    monkeypatch.setattr("app.gateway.control.diagnostic_log_transport.send_remote_gateway_management", send)
    task = asyncio.create_task(read_diagnostic_log_target(registry, payload.model_copy(update={"deadline_at": time() + 0.05}),
        gateway_id=REMOTE, request_id="req_remote_logs"))
    await body.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else httpx.ReadTimeout):
        await task
    assert body.closed and released.is_set()


async def test_ambiguous_gateway_connection_never_chooses_first(remote_logs, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, payload = remote_logs
    registry.upsert_remote_gateway(replace(registry.remote_gateway_connection("logs-remote"), connection_id="other-remote"))

    async def unexpected(**kwargs):
        raise AssertionError("连接不明确时不得隐式选取第一条管理路由")

    monkeypatch.setattr("app.gateway.control.diagnostic_log_transport.send_remote_gateway_management", unexpected)
    with pytest.raises(LookupError, match="不能唯一确定"):
        await read_diagnostic_log_target(registry, payload, gateway_id=REMOTE, request_id="req_remote_logs")
