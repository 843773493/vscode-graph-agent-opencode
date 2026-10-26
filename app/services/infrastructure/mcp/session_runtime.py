"""每个 Server 在自己的 task 中创建并关闭 SDK session。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, TypeAlias, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import StdioConnection, StreamableHttpConnection

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.invocation import CancellableMcpClientSession
from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools
from mcp import StdioServerParameters
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

McpChangeNotifyCallback: TypeAlias = Callable[[], None]


class McpServerSessionPort(Protocol):
    async def list_tools(self) -> list[BaseTool]: ...

    def supports_tool_list_changed(self) -> bool: ...


McpSessionFactory: TypeAlias = Callable[
    [McpServerConfig, McpChangeNotifyCallback],
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
    server: McpServerConfig, on_tools_list_changed: McpChangeNotifyCallback,
) -> AsyncIterator[McpServerSessionPort]:
    async def message_handler(message: object) -> None:
        if isinstance(message, mcp_types.ServerNotification) and isinstance(
            message.root, mcp_types.ToolListChangedNotification,
        ):
            on_tools_list_changed()

    @asynccontextmanager
    async def transport():
        if server.transport == "stdio":
            connection = cast(StdioConnection, server.connection)
            async with stdio_client(StdioServerParameters(
                command=connection["command"], args=connection["args"],
                env=connection.get("env"), cwd=connection.get("cwd"),
            )) as streams:
                yield streams
        else:
            connection = cast(StreamableHttpConnection, server.connection)
            async with (
                create_mcp_http_client(headers=connection.get("headers")) as client,
                streamable_http_client(connection["url"], http_client=client) as (read, write, _),
            ):
                yield read, write

    # 两种传输均使用 SDK，原 task 创建和退出 session，不保留旧适配器连接分路。
    async with transport() as (read, write), CancellableMcpClientSession(
        read, write, message_handler=message_handler,
    ) as session:
        await session.initialize()
        yield _McpServerSession(session)


class McpServerRuntime:
    def __init__(
        self, *, server: McpServerConfig, generation: int,
        session_factory: McpSessionFactory, on_change: McpChangeNotifyCallback,
        on_failure: Callable[[McpServerRuntime, BaseException], None], scope: LifetimeScope,
        on_closed: Callable[[McpServerRuntime, bool], None],
    ) -> None:
        self.server = server
        self.generation = generation
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
    def running(self) -> bool:
        return self._session is not None and not self._closing and self._failure is None

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
        await self._ready.wait()
        if self._failure is not None:
            raise self._failure

    async def _serve(self) -> None:
        try:
            async with self._session_factory(self.server, self._on_change) as session:
                self._session = session
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
            self._ready.set()
            self._on_closed(self, self._failure is None)

    def request_close(self) -> None:
        """同一发布屏障内同步撤销准入，SDK 关闭仍由原 task 完成。"""
        self._closing = True
        self._stop.set()

    async def close(self) -> None:
        self.request_close()
        if self._task is not None:
            if not self._ready.is_set():
                self._task.cancel()
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if not self._task.cancelled():
                    raise
        if self._close_failed:
            # 启动失败已由 start 报出；正常运行后的关闭失败须交付释放 owner。
            raise RuntimeError(f"MCP session 生命周期失败: server_id={self.server.server_id}") from self._failure
