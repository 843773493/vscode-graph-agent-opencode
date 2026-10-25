from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.config import McpServerConfig


class EchoToolInput(BaseModel):
    """MCP 远端工具测试替身的入参 schema 占位。"""

    text: str


def make_remote_tool(name: str) -> StructuredTool:
    """构造一个仅回显 ``"<name>:<text>"`` 的远端工具替身。"""

    async def _call(text: str) -> str:
        return f"{name}:{text}"

    return StructuredTool.from_function(
        coroutine=_call,
        name=name,
        description=f"{name} 工具",
        args_schema=EchoToolInput,
    )


class FakeMcpSession:
    """MCP 会话替身：``list_tools`` 只会依 ``tool_names`` 产出回显工具。

    ``relist_error`` 非空时 ``list_tools`` 抛出它，用于验证重新列举失败路径。
    """

    def __init__(self, *, tool_names: list[str], supports_notifications: bool = True):
        self.tool_names = list(tool_names)
        self.supports_notifications = supports_notifications
        self.relist_error: Exception | None = None
        self.list_calls = 0

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> list[BaseTool]:
        self.list_calls += 1
        if self.relist_error is not None:
            raise self.relist_error
        return [make_remote_tool(name) for name in self.tool_names]

    def supports_tool_list_changed(self) -> bool:
        return self.supports_notifications


class FakeMcpSessionFactory:
    """按 ``McpServerConfig`` 建 ``FakeMcpSession`` 的工厂替身，并记录回调。"""

    def __init__(self, initial_tools: dict[str, list[str]] | None = None):
        self._initial_tools = initial_tools or {}
        self.sessions: dict[str, FakeMcpSession] = {}
        self.notify_callbacks: dict[str, Callable[[], None]] = {}

    def __call__(
        self,
        server: McpServerConfig,
        on_tools_list_changed: Callable[[], None],
    ) -> object:
        session = FakeMcpSession(
            tool_names=list(self._initial_tools.get(server.server_id, [])),
        )
        self.sessions[server.server_id] = session
        self.notify_callbacks[server.server_id] = on_tools_list_changed

        @asynccontextmanager
        async def _session() -> AsyncIterator[FakeMcpSession]:
            yield session

        return _session()


async def drain_pending_relists(owner: McpCatalogOwner) -> None:
    """等待 owner 内部所有待处理的重新列举任务结束。"""

    tasks = tuple(owner._pending_relist_tasks)
    if tasks:
        await asyncio.gather(*tasks)


__all__ = [
    "EchoToolInput",
    "FakeMcpSession",
    "FakeMcpSessionFactory",
    "drain_pending_relists",
    "make_remote_tool",
]
