"""每个 Server 在自己的 task 中创建并关闭 SDK session。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import StdioConnection, StreamableHttpConnection

from app.core.child_process import ChildProcessState, OwnedChildProcess
from app.core.lifecycle import LifetimeScope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.invocation import CancellableMcpClientSession
from app.services.infrastructure.mcp.oauth import (
    McpOAuthProvider,
    is_authorization_required,
)
from app.services.infrastructure.mcp.private_inputs import (
    McpPrivateInputStore,
    candidate_env_definition,
    stdio_env_definition,
)
from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools
from app.services.infrastructure.mcp.transport.stdio import mcp_stdio_transport
from mcp import StdioServerParameters
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

if TYPE_CHECKING:
    from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner

McpChangeNotifyCallback: TypeAlias = Callable[[], None]


@dataclass(frozen=True, slots=True)
class McpSessionLifecycle:
    process: OwnedChildProcess
    on_process_started: Callable[[], Awaitable[None]]


class McpServerSessionPort(Protocol):
    async def list_tools(self) -> list[BaseTool]: ...

    def supports_tool_list_changed(self) -> bool: ...


McpSessionFactory: TypeAlias = Callable[
    [McpServerConfig, McpChangeNotifyCallback, McpSessionLifecycle],
    AbstractAsyncContextManager[McpServerSessionPort],
]


class _McpServerSession:
    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def list_tools(self) -> list[BaseTool]:
        return await list_mcp_tools(self._session)

    def supports_tool_list_changed(self) -> bool:
        capabilities = self._session.get_server_capabilities()
        return bool(capabilities and capabilities.tools and capabilities.tools.listChanged)


@asynccontextmanager
async def production_mcp_session_factory(
    server: McpServerConfig, on_tools_list_changed: McpChangeNotifyCallback, lifecycle: McpSessionLifecycle,
    *, workspace_root: Path | None = None, oauth_provider: McpOAuthProvider | None = None,
    installation_owner: McpInstallationOwner | None = None,
) -> AsyncIterator[McpServerSessionPort]:
    auth = oauth_provider
    if server.oauth is not None and auth is None:
        if workspace_root is None:
            raise ValueError("MCP OAuth session 必须显式指定所属工作区")
        auth = McpOAuthProvider(server=server, workspace_root=workspace_root)
    async def message_handler(message: object) -> None:
        if isinstance(message, mcp_types.ServerNotification) and isinstance(
            message.root, mcp_types.ToolListChangedNotification,
        ):
            on_tools_list_changed()

    @asynccontextmanager
    async def transport():
        if server.transport == "stdio":
            connection = cast(StdioConnection, server.connection)
            if server.managed_launch is not None:
                if installation_owner is None:
                    raise ValueError("受管 MCP launch 缺少原生 Backend 安装 owner")
                command = await asyncio.to_thread(installation_owner.resolve, server.managed_launch, verify=True)
                if connection["command"] != command:
                    raise ValueError("受管 MCP 连接配置与已发布 generation 的实际启动命令不一致")
            env = dict(connection.get("env") or {})
            if server.private_env:
                if workspace_root is None:
                    raise ValueError("MCP 私有 env session 必须显式指定所属工作区")
                definition = stdio_env_definition(connection)
                if server.managed_launch is not None:
                    assert installation_owner is not None
                    artifact = await asyncio.to_thread(installation_owner.artifact, server.managed_launch)
                    definition = candidate_env_definition(artifact.source, artifact.manifest_digest, artifact.variant)
                store = McpPrivateInputStore(workspace_root=workspace_root)
                for reference in server.private_env:
                    env[reference.input_key] = await asyncio.to_thread(store.resolve_env, reference,
                        server_id=server.server_id, input_key=reference.input_key, definition_digest=definition)
            async with mcp_stdio_transport(StdioServerParameters(
                command=connection["command"], args=connection["args"],
                env=env, cwd=connection.get("cwd"),
            ), lifecycle.process, lifecycle.on_process_started) as streams:
                yield streams
        else:
            connection = cast(StreamableHttpConnection, server.connection)
            headers = dict(connection.get("headers") or {})
            if server.private_headers:
                if workspace_root is None:
                    raise ValueError("MCP 私有 header session 必须显式指定所属工作区")
                store = McpPrivateInputStore(workspace_root=workspace_root)
                for reference in server.private_headers:
                    headers[reference.header_name] = await asyncio.to_thread(
                        store.resolve, reference, server_id=server.server_id,
                        header_name=reference.header_name, endpoint=connection["url"],
                    )
            async with create_mcp_http_client(headers=headers, auth=auth) as client:
                if server.private_headers:
                    # 引用只授权精确 endpoint；不能随 SDK 默认重定向把任意 header 交给另一地址。
                    client.follow_redirects = False
                async with streamable_http_client(connection["url"], http_client=client) as (read, write, _):
                    yield read, write

    # 两种传输均使用 SDK，原 task 创建和退出 session，不保留旧适配器连接分路。
    async with transport() as (read, write), CancellableMcpClientSession(
        read, write, message_handler=message_handler,
    ) as session:
        await session.initialize()
        yield _McpServerSession(session)


class McpServerRuntime:
    def __init__(
        self, *, server: McpServerConfig, generation: int, connection_ref: str,
        session_factory: McpSessionFactory, on_change: McpChangeNotifyCallback,
        on_failure: Callable[[McpServerRuntime, BaseException], None], scope: LifetimeScope,
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
        self.lifecycle = McpSessionLifecycle(OwnedChildProcess(connection_ref), lambda: self._on_preparing(self))
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
    def process_state(self) -> ChildProcessState | None:
        return self.lifecycle.process.state if self.server.transport == "stdio" else None

    @property
    def running(self) -> bool:
        return self._session is not None and not self._closing and self._failure is None

    @property
    def authorization_required(self) -> bool:
        return self._failure is not None and is_authorization_required(self._failure)

    @property
    def session(self) -> McpServerSessionPort:
        if not self.running or self._session is None:
            raise RuntimeError(f"MCP session 不可用: server_id={self.server.server_id}") from self._failure
        return self._session

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("MCP Server runtime 不允许重复启动")
        self._task = asyncio.create_task(
            self._serve(), name=f"mcp-session:{self.server.server_id}:{self.generation}",
        )
        try:
            await self._ready.wait()
        except asyncio.CancelledError as original:
            # 登记可能正在worker中写盘；取消入口须等原task关闭并提交终态。
            try:
                await run_cleanup_shielded(self.close)
            except BaseException as failure:
                raise original from failure
            raise
        if self._failure is not None:
            raise self._failure
        if not self.running:
            raise RuntimeError(f"MCP session 启动完成前已关闭: server_id={self.server.server_id}")

    async def _serve(self) -> None:
        try:
            await self._on_preparing(self)
            async with self._session_factory(self.server, self._on_change, self.lifecycle) as session:
                self._session = session
                await self._on_started(self)
                self._ready.set()
                await self._stop.wait()
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - task 边界必须向 owner 交付所有 SDK 生命周期失败
            if not (self._closing and isinstance(error, asyncio.CancelledError)):
                # 失败传给 start、close 或 owner 的失效发布，绝不作为成功吞掉。
                self._failure = error
                self._close_failed = self._closing
                if self._ready.is_set() and not self._closing:
                    self._on_failure(self, error)
        finally:
            self._session = None
            try:
                if self.process_state is not None and self.process_state.phase == "reserved":
                    # 工厂在进入传输前已拒绝／取消；尚无实际进程，核实未创建事实。
                    await run_cleanup_shielded(self.lifecycle.process.close)
                # 协议失败不改变原进程已退出的事实；未知调用仍由原 invocation lease 保留。
                process = self.process_state
                verified = process.exit_verified if process is not None else self._failure is None
                await self._on_closed(self, verified)
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - 原task保存持久终态失败，交回start/close。
                self._close_failed = True
                self._failure = error if self._failure is None else BaseExceptionGroup(
                    "MCP session 与终态登记均失败", [self._failure, error],
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
            raise RuntimeError(f"MCP session 生命周期失败: server_id={self.server.server_id}") from self._failure
