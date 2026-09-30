from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.workspace_proxy import _proxy_workspace_request


@pytest.fixture(autouse=True)
def _hermetic_gateway_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """隔离 Gateway 控制面根目录并清掉本机代理环境变量，避免污染真实用户数据。"""
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway_root"))
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _make_request(
    application: FastAPI,
    *,
    method: str,
    path: str,
    workspace_id: str,
) -> Request:
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(b"x-boxteam-workspace-id", workspace_id.encode())],
            "app": application,
        }
    )
    request.state.request_id = "req_route_ref"
    return request


@pytest.mark.asyncio
async def test_remote_credential_failure_releases_route_reference(
    tmp_path: Path,
) -> None:
    """F1：远程目标联邦凭据缺失导致 header 构造失败时，路由引用必须归还。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_remote",
            name="remote",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="remote_gateway",
            remote_gateway_connection_id="conn-missing",
            remote_workspace_id="remote-ws",
        ),
        activate=False,
    )
    registry._remote_gateway_runtimes["conn-missing"] = _FakeRuntime(
        {"workspace_api": "http://127.0.0.1:41001"}
    )
    client = httpx.AsyncClient()
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = _make_request(
        application,
        method="GET",
        path="/api/v1/workspace",
        workspace_id="gw_remote",
    )

    try:
        with pytest.raises(LookupError):
            await _proxy_workspace_request(
                "workspace",
                request,
                auth=None,
                user_access=None,
                include_credentials=True,
            )
    finally:
        await client.aclose()

    assert registry.route_reference_counts("gw_remote") == (0, 0)
    registry._assert_route_references_drained("gw_remote")


@pytest.mark.asyncio
async def test_message_stream_retry_resolve_failure_releases_route_reference(
    tmp_path: Path,
) -> None:
    """F2：message-stream 重试期间工作区被移除、resolve 抛错时必须归还引用。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        activate=False,
    )
    first_response_delivered = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_response_delivered
        first_response_delivered = True
        # 首次订阅返回 404，触发 Gateway 内部重试窗口。
        return httpx.Response(404, headers={"content-type": "application/json"})

    original_resolve = registry.resolve

    def resolve_or_die(workspace_id: str | None = None) -> WorkspaceTarget:
        # 进入重试窗口后工作区被并发移除，重试块里的 resolve 必须响亮抛错。
        if first_response_delivered:
            raise LookupError("workspace removed mid-retry")
        return original_resolve(workspace_id)

    registry.resolve = resolve_or_die  # type: ignore[method-assign]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = _make_request(
        application,
        method="GET",
        path="/api/v1/sessions/ses_1/turns/job_1/message-stream",
        workspace_id="gw_stream",
    )

    try:
        with pytest.raises(LookupError):
            await _proxy_workspace_request(
                "sessions/ses_1/turns/job_1/message-stream",
                request,
                auth=None,
                user_access=None,
                include_credentials=False,
            )
    finally:
        await client.aclose()

    registry.resolve = original_resolve  # type: ignore[method-assign]
    assert registry.route_reference_counts("gw_stream") == (0, 0)
    registry._assert_route_references_drained("gw_stream")


@pytest.mark.asyncio
async def test_client_disconnect_releases_route_reference(
    tmp_path: Path,
) -> None:
    """F3：客户端在上游挂起期间断连（handler 被取消）时必须归还路由引用。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_hang",
            name="hang",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        activate=False,
    )
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        # 模拟「已接受连接但永不回写响应头」的上游。
        await asyncio.Event().wait()
        raise AssertionError("上游挂起分支不应返回响应")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = _make_request(
        application,
        method="GET",
        path="/api/v1/workspace",
        workspace_id="gw_hang",
    )

    try:
        with pytest.raises(asyncio.TimeoutError):
            # 客户端断连会让 Starlette 取消 handler 任务；这里用测试侧超时
            # 在进程内等价地取消同一个协程。
            await asyncio.wait_for(
                _proxy_workspace_request(
                    "workspace",
                    request,
                    auth=None,
                    user_access=None,
                    include_credentials=False,
                ),
                timeout=0.2,
            )
    finally:
        await client.aclose()

    assert entered.is_set() is True
    assert registry.route_reference_counts("gw_hang") == (0, 0)
    registry._assert_route_references_drained("gw_hang")


class _FakeRuntime:
    def __init__(self, service_urls: dict[str, str]) -> None:
        self.service_urls = service_urls
