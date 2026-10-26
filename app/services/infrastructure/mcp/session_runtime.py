"""每个 Server 在自己的 task 中创建并关闭 SDK session。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, TypeAlias, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection, create_session

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.tool_adapter import list_mcp_tools
from mcp import types as mcp_types
from mcp.client.session import ClientSession

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

    # Server 配置已由唯一 parser 校验；此处补 SDK 专属回调后收窄 TypedDict。
    connection = cast(Connection, {
        **server.connection, "session_kwargs": {"message_handler": message_handler},
    })
    async with create_session(connection) as session:
        await session.initialize()
        yield _McpServerSession(session)


class McpServerRuntime:
    def __init__(
        self, *, server: McpServerConfig, generation: int,
        session_factory: McpSessionFactory, on_change: McpChangeNotifyCallback,
        on_failure: Callable[[McpServerRuntime, BaseException], None], scope: LifetimeScope,
    ) -> None:
        self.server = server
        self.generation = generation
        self.last_relist_error: str | None = None
        self.relist_sequence = 0
        self._session_factory = session_factory
        self._on_change = on_change
        self._on_failure = on_failure
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

    async def close(self) -> None:
        self._closing = True
        self._stop.set()
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
