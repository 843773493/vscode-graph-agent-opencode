"""每个 Server 在自己的 task 中创建并关闭 SDK session。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias

import anyio
import httpx
from langchain_core.tools import BaseTool

from app.core.lifecycle import LifetimeScope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.workspace_identity import read_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.invocation import CancellableMcpClientSession
from app.services.infrastructure.mcp.oauth import (
    McpOAuthProvider,
    is_authorization_required,
)
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore
from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools
from app.services.infrastructure.mcp.tool_definitions import PluginToolSource
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService

McpChangeNotifyCallback: TypeAlias = Callable[[], None]


class McpServerSessionPort(Protocol):
    async def list_tools(self) -> list[BaseTool]: ...

    def supports_tool_list_changed(self) -> bool: ...


McpSessionFactory: TypeAlias = Callable[
    [McpServerConfig, McpChangeNotifyCallback],
    AbstractAsyncContextManager[McpServerSessionPort],
]


class _GatewayMcpTransport(httpx.AsyncBaseTransport):
    """只在传输边界换目标，MCP/OAuth SDK 始终看到原始请求 URL。"""

    def __init__(self, *, gateway_url: str, endpoint_url: str, endpoint_headers: dict[str, str],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport or httpx.AsyncHTTPTransport(trust_env=False)
        self._proxy_url = httpx.URL(gateway_url)
        self._endpoint_url = httpx.URL(endpoint_url).copy_with(query=None)
        self._endpoint_headers = endpoint_headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target_url = request.url
        headers = dict(request.headers.items())
        if target_url.copy_with(query=None) == self._endpoint_url:
            headers.update(self._endpoint_headers)
        headers["X-Local-Token"] = get_gateway_local_token()
        headers["X-BoxTeam-MCP-Endpoint"] = str(target_url.copy_with(query=None))
        proxy_request = httpx.Request(
            request.method,
            self._proxy_url.copy_merge_params(tuple(target_url.params.multi_items())),
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        response = await self._transport.handle_async_request(proxy_request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_GatewayMcpResponseStream(response),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class _GatewayMcpResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aiter__(self):
        async for chunk in self._response.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _McpServerSession:
    def __init__(self, session: ClientSession, source: PluginToolSource | None = None) -> None:
        self._session = session
        self._source = source

    async def list_tools(self) -> list[BaseTool]:
        return await list_mcp_tools(self._session, source=self._source)

    def supports_tool_list_changed(self) -> bool:
        capabilities = self._session.get_server_capabilities()
        return bool(
            capabilities and capabilities.tools and capabilities.tools.listChanged
        )


@asynccontextmanager
async def production_mcp_session_factory(
    server: McpServerConfig,
    on_tools_list_changed: McpChangeNotifyCallback,
    *,
    workspace_root: Path | None = None,
    oauth_provider: McpOAuthProvider | None = None,
    config_service: ConfigService | None = None,
) -> AsyncIterator[McpServerSessionPort]:
    auth = oauth_provider
    route_identity: dict[str, str] = {}
    route_invalidated = asyncio.Event()
    route_error: McpPluginInstanceChanged | None = None
    if server.oauth is not None and auth is None:
        if workspace_root is None:
            raise ValueError("MCP OAuth session 必须显式指定所属工作区")
        auth = McpOAuthProvider(server=server, workspace_root=workspace_root)

    if server.transport == "plugin":
        service_ref = server.service
        if service_ref is None or config_service is None or workspace_root is None:
            raise ValueError("MCP 插件服务连接缺少原 Workspace 配置 owner 或服务引用")
        caller_workspace_id = read_workspace_id(workspace_root)
        if service_ref.workspace_id is not None and service_ref.workspace_id != caller_workspace_id:
            raise ValueError("MCP 插件实例不属于当前工作区")
        url = GatewayTransportConnection(
            gateway_url=os.environ.get("BOXTEAM_GATEWAY_URL"),
            config_service=config_service
        ).resolve_gateway_url()
        url += (
            f"/api/gateway/service-plugins/{service_ref.package_id}/{service_ref.plugin_id}"
            f"/services/{service_ref.service_id}/?package_version={service_ref.package_version}"
        )
        if service_ref.workspace_id is not None:
            url += f"&workspace_id={service_ref.workspace_id}"
        proxy_url = url
        session_url = url
        headers = {
            "X-Local-Token": get_gateway_local_token(),
            "X-BoxTeam-Workspace-Id": caller_workspace_id,
            "X-BoxTeam-Gateway-Id": service_ref.gateway_id,
            TARGET_GATEWAY_ID_HEADER: service_ref.gateway_id,
        }
        async def pin_instance(request: httpx.Request) -> None:
            instance_id = route_identity.get("instance_id")
            if instance_id is not None:
                request.headers["X-BoxTeam-Plugin-Instance-Id"] = instance_id

        async def capture_instance(response: httpx.Response) -> None:
            nonlocal route_error
            detail = None
            if not response.is_success:
                if route_identity and response.status_code in {401, 403, 404, 409}:
                    detail = f"Gateway MCP 原插件连接已失效: HTTP {response.status_code}"
            elif response.headers.get("X-BoxTeam-Plugin-Gateway-Id") != service_ref.gateway_id:
                detail = "Gateway MCP 插件响应来自不同目标"
            else:
                for key, header in (("instance_id", "X-BoxTeam-Plugin-Instance-Id"),
                                    ("package_digest", "X-BoxTeam-Plugin-Package-Digest")):
                    value = response.headers.get(header)
                    if not value:
                        detail = f"Gateway MCP 插件响应缺少实际来源身份: {header}"
                        break
                    if route_identity.setdefault(key, value) != value:
                        detail = "Gateway MCP 请求改投了不同插件实例或包"
                        break
            if detail is not None:
                route_error = McpPluginInstanceChanged(detail)
                route_invalidated.set()
                raise route_error

        client = httpx.AsyncClient(
            headers=headers,
            auth=auth,
            follow_redirects=False,
            timeout=httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            transport=httpx.AsyncHTTPTransport(trust_env=False),
        )
        client.event_hooks["request"].append(pin_instance)
        client.event_hooks["response"].append(capture_instance)
    else:
        connection = server.connection
        if connection is None or connection["transport"] != "streamable_http":
            raise ValueError("MCP 外部 HTTP 配置缺少 Streamable HTTP endpoint")
        external_url = connection["url"]
        endpoint_headers = dict(connection.get("headers") or {})
        if server.private_headers:
            if workspace_root is None:
                raise ValueError("MCP 私有 header session 必须显式指定所属工作区")
            store = McpPrivateInputStore(workspace_root=workspace_root)
            for reference in server.private_headers:
                endpoint_headers[reference.header_name] = await asyncio.to_thread(
                    store.resolve,
                    reference,
                    server_id=server.server_id,
                    header_name=reference.header_name,
                    endpoint=external_url,
                )
        if config_service is None:
            raise ValueError("外部 MCP 连接缺少 Gateway 配置 owner")
        gateway_url = GatewayTransportConnection(
            gateway_url=os.environ.get("BOXTEAM_GATEWAY_URL"),
            config_service=config_service,
        ).resolve_gateway_url()
        proxy_url = f"{gateway_url}/api/gateway/mcp/upstream"
        client = httpx.AsyncClient(
            auth=auth,
            follow_redirects=False,
            timeout=httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            transport=_GatewayMcpTransport(
                gateway_url=proxy_url,
                endpoint_url=external_url,
                endpoint_headers=endpoint_headers,
            ),
        )
        session_url = external_url

    async def message_handler(message: object) -> None:
        if isinstance(message, mcp_types.ServerNotification) and isinstance(
            message.root,
            mcp_types.ToolListChangedNotification,
        ):
            on_tools_list_changed()

    transport_failure: BaseException | None = None

    async def watch_plugin_route() -> None:
        # SDK 会吞掉后台 GET 重连异常；沿同一响应判失效，结束原 session，不能继续伪报 ready。
        await route_invalidated.wait()
        assert route_error is not None
        raise route_error

    async with client:
        try:
            async with anyio.create_task_group() as group:
                if server.service is not None:
                    group.start_soon(watch_plugin_route)
                try:
                    async with (
                        streamable_http_client(session_url, http_client=client) as (read, write, _),
                        CancellableMcpClientSession(read, write, message_handler=message_handler) as session,
                    ):
                        await session.initialize()
                        source = None
                        if server.service is not None:
                            source = PluginToolSource.from_metadata({
                                **server.service.model_dump(), **route_identity, "contract_version": 1,
                            })
                        yield _McpServerSession(session, source)
                finally:
                    group.cancel_scope.cancel()
        except* (httpx.HTTPError, McpPluginInstanceChanged) as error:
            # SDK task group 已排空；还须等外层 client 正常关闭，才能确认本地释放。
            # 混合异常的其余原因仍直接抛出，不把真实清理失败归为通信失败。
            transport_failure = error
    if transport_failure is not None:
        raise _McpTransportClosed("MCP 通信失败，本地 HTTP 连接已关闭") from transport_failure


class _McpTransportClosed(RuntimeError):
    """原通信异常仍在 cause 中；此标记仅确认 SDK 与 HTTP client 已释放。"""


class McpPluginInstanceChanged(RuntimeError):
    """插件配置或运行实例在 Backend connection generation 中发生变化。"""


class McpServerRuntime:
    def __init__(
        self,
        *,
        server: McpServerConfig,
        generation: int,
        connection_ref: str,
        session_factory: McpSessionFactory,
        on_change: McpChangeNotifyCallback,
        on_failure: Callable[[McpServerRuntime, BaseException], None],
        scope: LifetimeScope,
        on_started: Callable[[McpServerRuntime], Awaitable[None]],
        on_preparing: Callable[[McpServerRuntime], Awaitable[None]],
        on_closed: Callable[[McpServerRuntime, bool], Awaitable[None]],
    ) -> None:
        self.server = server
        self.generation = generation
        self.connection_ref = connection_ref
        self.scope = scope
        self.last_relist_error: str | None = None
        self.relist_sequence = 0
        self._session_factory = session_factory

        def changed() -> None:
            # 候选尚未公开时也记录通知；发布不能把过期 relist 伪报为就绪。
            self.relist_sequence += 1
            on_change()

        self._on_change = changed
        self._on_failure = on_failure
        self._on_started = on_started
        self._on_preparing = on_preparing
        self._on_closed = on_closed
        self._session: McpServerSessionPort | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._closing = False
        self._failure: BaseException | None = None
        self._close_failed = False
        self._task: asyncio.Task[None] | None = None
        # scope 释放回调持有并排空原 task，不能在释放 task 中退出 SDK 上下文。
        scope.register(self.close, label=f"mcp-session:{server.server_id}:{generation}")

    @property
    def cleanup_failed(self) -> bool:
        return self._close_failed

    @property
    def running(self) -> bool:
        return self._session is not None and not self._closing and self._failure is None

    @property
    def authorization_required(self) -> bool:
        return self._failure is not None and is_authorization_required(self._failure)

    @property
    def session(self) -> McpServerSessionPort:
        if not self.running or self._session is None:
            raise RuntimeError(
                f"MCP session 不可用: server_id={self.server.server_id}"
            ) from self._failure
        return self._session

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("MCP Server runtime 不允许重复启动")
        self._task = asyncio.create_task(
            self._serve(),
            name=f"mcp-session:{self.server.server_id}:{self.generation}",
        )
        try:
            await self._ready.wait()
        except asyncio.CancelledError as original:
            # 登记可能正在worker中写盘；取消入口须等原task关闭并提交终态。
            try:
                await run_cleanup_shielded(self.close)
            except BaseException as failure:
                raise original from failure
            if self._failure is not None:
                raise original from self._failure
            raise
        if self._failure is not None:
            raise self._failure
        if not self.running:
            raise RuntimeError(
                f"MCP session 启动完成前已关闭: server_id={self.server.server_id}"
            )

    async def _serve(self) -> None:
        startup_failure: Exception | None = None
        session_closed_verified = False
        session_entered = False
        try:
            await self._on_preparing(self)
            async with self._session_factory(self.server, self._on_change) as session:
                self._session = session
                session_entered = True
                try:
                    await self._on_started(self)
                except Exception as error:  # noqa: BLE001 - 原登记异常须在 SDK context 正常关闭后交回 owner。
                    # 让原 session context 正常退出，再把登记失败交回 owner；
                    # 否则 SDK 会把它当作清理异常重新包装。
                    startup_failure = error
                if startup_failure is None:
                    self._ready.set()
                    await self._stop.wait()
            session_closed_verified = True
            if startup_failure is not None:
                raise startup_failure
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - task 边界必须向 owner 交付所有 SDK 生命周期失败
            session_closed_verified = session_closed_verified or isinstance(error, _McpTransportClosed)
            if startup_failure is not None and error is not startup_failure:
                self._close_failed = True
                error = BaseExceptionGroup(
                    "MCP session 启动登记与关闭均失败",
                    [startup_failure, error],
                )
            if not (self._closing and isinstance(error, asyncio.CancelledError)):
                # 失败传给 start、close 或 owner 的失效发布，绝不作为成功吞掉。
                self._failure = error
            self._close_failed = self._close_failed or (
                session_entered and not session_closed_verified
                and not isinstance(error, asyncio.CancelledError)
            )
            if self._ready.is_set() and not self._closing:
                self._on_failure(self, error)
        finally:
            self._session = None
            try:
                await self._on_closed(self, self._failure is None or session_closed_verified)
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - 原task保存持久终态失败，交回start/close。
                self._close_failed = True
                self._failure = (
                    error
                    if self._failure is None
                    else BaseExceptionGroup(
                        "MCP session 与终态登记均失败",
                        [self._failure, error],
                    )
                )
                if self._ready.is_set() and not self._closing:
                    self._on_failure(self, self._failure)
            finally:
                self._ready.set()

    def request_close(self) -> None:
        """同一发布屏障内同步撤销准入，SDK 关闭仍由原 task 完成。"""
        if self._closing:
            return
        self._closing = True
        self._stop.set()
        # 初始化只中断一次；后续关闭入口等待同一 task，不能再次打断 SDK 清理。
        if self._task is not None and not self._ready.is_set():
            self._task.cancel()

    async def close(self) -> None:
        self.request_close()
        if self._task is not None:
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if not self._task.cancelled():
                    raise
        if self._close_failed:
            # 启动失败已由 start 报出；正常运行后的关闭失败须交付释放 owner。
            raise RuntimeError(
                f"MCP session 生命周期失败: server_id={self.server.server_id}"
            ) from self._failure
