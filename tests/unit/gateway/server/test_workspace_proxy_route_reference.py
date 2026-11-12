from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceSelector,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.workspace_proxy import _proxy_workspace_request

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


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
    target_gateway_id: str | None = None,
) -> Request:
    headers = [(b"x-boxteam-workspace-id", workspace_id.encode())]
    if target_gateway_id is not None:
        headers.append((TARGET_GATEWAY_ID_HEADER.lower().encode(), target_gateway_id.encode()))
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
            "app": application,
        }
    )
    request.state.request_id = "req_route_ref"
    return request


@pytest.mark.asyncio
async def test_local_proxy_validates_and_forwards_canonical_workspace_identity(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway_root" / "workspaces.json")
    target = registry.upsert(
        WorkspaceTarget(
            gateway_id=registry.gateway_id,
            workspace_id=WORKSPACE_ID,
            name="workspace",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        activate=False,
    )
    forwarded: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert registry.route_reference_counts(target.key) == (1, 0)
        forwarded.append(request)
        return httpx.Response(200, json={"request_id": "req_proxy", "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application = FastAPI()
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    matching_request = _make_request(
        application,
        method="GET",
        path=f"/api/v1/workspaces/{WORKSPACE_ID}/workspace",
        workspace_id=WORKSPACE_ID,
    )

    try:
        response = await _proxy_workspace_request(
            f"workspaces/{WORKSPACE_ID}/workspace",
            matching_request,
            auth=None,
            include_credentials=False,
        )
        assert response.status_code == 200
        outgoing, = forwarded
        assert outgoing.url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/workspace"
        assert outgoing.headers["x-boxteam-workspace-id"] == WORKSPACE_ID
        assert outgoing.headers["X-BoxTeam-Gateway-Id"] == registry.gateway_id
        assert outgoing.headers["X-Request-ID"] == "req_route_ref"
        assert isinstance(response, StreamingResponse)
        assert registry.route_reference_counts(target.key) == (1, 0)
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks
        assert registry.route_reference_counts(target.key) == (0, 0)

        mismatching_request = _make_request(
            application,
            method="GET",
            path=f"/api/v1/workspaces/{WORKSPACE_ID}/workspace",
            workspace_id="00000000-0000-4000-8000-000000000002",
        )
        with pytest.raises(HTTPException) as error:
            await _proxy_workspace_request(
                f"workspaces/{WORKSPACE_ID}/workspace",
                mismatching_request,
                auth=None,
                include_credentials=False,
            )
        assert error.value.status_code == 400
        assert error.value.detail == "路径与请求头的工作区 UUID 不一致"
        assert len(forwarded) == 1
        assert registry.route_reference_counts(target.key) == (0, 0)
    finally:
        await client.aclose()
        registry.close()


@pytest.mark.asyncio
async def test_remote_credential_failure_releases_route_reference(
    tmp_path: Path,
) -> None:
    """F1：远程目标联邦凭据缺失导致 header 构造失败时，路由引用必须归还。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway_root" / "workspaces.json")
    connection = RemoteGatewayConnection(
        connection_id="conn-missing", name="未配对远端", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id="gateway_" + "e" * 32, protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual", remote_config_event_cursor=1,
    )
    target = WorkspaceTarget(
        gateway_id=connection.remote_gateway_id,
        workspace_id=WORKSPACE_ID,
        name="remote",
        root_path=None,
        backend_url="http://127.0.0.1:41001",
        connection_kind="remote_gateway",
        owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
    )
    registry.apply_remote_projection_snapshot(
        connection=connection, projections=(target,),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://127.0.0.1:41001"}),
    )
    forwarded: list[httpx.Request] = []

    def unexpected_upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        raise AssertionError("缺少联邦凭据时不得进入上游")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_upstream))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = _make_request(
        application,
        method="GET",
        path="/api/v1/workspace",
        workspace_id=WORKSPACE_ID,
        target_gateway_id=connection.remote_gateway_id,
    )

    try:
        with pytest.raises(LookupError):
            await _proxy_workspace_request(
                "workspace",
                request,
                auth=None,
                include_credentials=True,
            )
    finally:
        await client.aclose()
        registry.close()

    assert forwarded == []
    assert registry.route_reference_counts(target.key) == (0, 0)
    registry._assert_route_references_drained(target.key)


@pytest.mark.asyncio
async def test_message_stream_retry_resolve_failure_releases_route_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2：message-stream 重试期间工作区被移除、resolve 抛错时必须归还引用。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway_root" / "workspaces.json")
    target = registry.upsert(
        WorkspaceTarget(
            gateway_id=registry.gateway_id,
            workspace_id=WORKSPACE_ID,
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
        assert registry.route_reference_counts(target.key) == (1, 0)
        first_response_delivered = True
        # 首次订阅返回 404，触发 Gateway 内部重试窗口。
        return httpx.Response(404, headers={"content-type": "application/json"})

    original_resolve = registry.resolve

    def resolve_or_die(workspace_id: WorkspaceSelector | None = None) -> WorkspaceTarget:
        # 进入重试窗口后工作区被并发移除，重试块里的 resolve 必须响亮抛错。
        if first_response_delivered:
            raise LookupError("workspace removed mid-retry")
        return original_resolve(workspace_id)

    monkeypatch.setattr(registry, "resolve", resolve_or_die)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = _make_request(
        application,
        method="GET",
        path="/api/v1/sessions/ses_1/turns/job_1/message-stream",
        workspace_id=WORKSPACE_ID,
    )

    try:
        with pytest.raises(LookupError, match="workspace removed mid-retry"):
            await _proxy_workspace_request(
                "sessions/ses_1/turns/job_1/message-stream",
                request,
                auth=None,
                include_credentials=False,
            )
    finally:
        monkeypatch.setattr(registry, "resolve", original_resolve)
        await client.aclose()
        registry.close()

    assert registry.route_reference_counts(target.key) == (0, 0)
    registry._assert_route_references_drained(target.key)


@pytest.mark.asyncio
async def test_client_disconnect_releases_route_reference(
    tmp_path: Path,
) -> None:
    """F3：客户端在上游挂起期间断连（handler 被取消）时必须归还路由引用。"""

    application = FastAPI()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway_root" / "workspaces.json")
    target = registry.upsert(
        WorkspaceTarget(
            gateway_id=registry.gateway_id,
            workspace_id=WORKSPACE_ID,
            name="hang",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        activate=False,
    )
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert registry.route_reference_counts(target.key) == (1, 0)
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
        workspace_id=WORKSPACE_ID,
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
                    include_credentials=False,
                ),
                timeout=0.2,
            )
    finally:
        await client.aclose()
        registry.close()

    assert entered.is_set() is True
    assert registry.route_reference_counts(target.key) == (0, 0)
    registry._assert_route_references_drained(target.key)
