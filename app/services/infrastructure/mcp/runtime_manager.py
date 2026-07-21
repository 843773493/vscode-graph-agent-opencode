from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from app.services.infrastructure.mcp.config import (
    McpServerConfig,
    parse_mcp_server_configs,
)
from app.services.infrastructure.mcp.naming import build_mcp_tool_id


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    tool_id: str
    server_id: str
    remote_name: str
    description: str


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    server_id: str
    transport: str
    enabled: bool
    status: str
    tools: tuple[McpToolDescriptor, ...]


class McpRuntimeManager:
    def __init__(
        self,
        *,
        raw_config: object,
        workspace_root: Path,
    ) -> None:
        self._servers = parse_mcp_server_configs(
            raw_config,
            workspace_root=workspace_root,
        )
        self._clients: dict[str, MultiServerMCPClient] = {}
        self._tools_by_id: dict[str, BaseTool] = {}
        self._descriptors_by_server: dict[str, tuple[McpToolDescriptor, ...]] = {}
        self._session_stack: AsyncExitStack | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("McpRuntimeManager 不允许重复启动")

        session_stack = AsyncExitStack()
        await session_stack.__aenter__()
        try:
            for server in self._servers:
                if not server.enabled:
                    continue
                client = MultiServerMCPClient(
                    {server.server_id: server.connection},
                    handle_tool_errors=True,
                )
                session = await session_stack.enter_async_context(
                    client.session(server.server_id)
                )
                remote_tools = await load_mcp_tools(
                    session,
                    callbacks=client.callbacks,
                    server_name=server.server_id,
                    handle_tool_errors=True,
                )

                descriptors: list[McpToolDescriptor] = []
                for remote_tool in remote_tools:
                    tool_id = build_mcp_tool_id(server.server_id, remote_tool.name)
                    if tool_id in self._tools_by_id:
                        raise ValueError(
                            "MCP 工具命名冲突，禁止静默覆盖: "
                            f"server_id={server.server_id} remote_name={remote_tool.name} "
                            f"tool_id={tool_id}"
                        )
                    metadata = {
                        **dict(remote_tool.metadata or {}),
                        "mcp_server_id": server.server_id,
                        "mcp_remote_tool_name": remote_tool.name,
                    }
                    adapted_tool = remote_tool.model_copy(
                        update={"name": tool_id, "metadata": metadata}
                    )
                    self._tools_by_id[tool_id] = adapted_tool
                    descriptors.append(
                        McpToolDescriptor(
                            tool_id=tool_id,
                            server_id=server.server_id,
                            remote_name=remote_tool.name,
                            description=remote_tool.description or "",
                        )
                    )
                self._clients[server.server_id] = client
                self._descriptors_by_server[server.server_id] = tuple(descriptors)
        except Exception as error:
            await session_stack.aclose()
            self._clients.clear()
            self._tools_by_id.clear()
            self._descriptors_by_server.clear()
            raise RuntimeError(f"MCP Server 启动或工具发现失败: {error}") from error

        # TODO: 处理 notifications/tools/list_changed，并在下一轮重建 Agent 工具目录。
        self._session_stack = session_stack
        self._started = True

    async def shutdown(self) -> None:
        if self._session_stack is not None:
            await self._session_stack.aclose()
            self._session_stack = None
        self._clients.clear()
        self._tools_by_id.clear()
        self._descriptors_by_server.clear()
        self._started = False

    def get_tools(self) -> list[BaseTool]:
        self._require_started()
        return list(self._tools_by_id.values())

    def get_tool_ids(self) -> frozenset[str]:
        self._require_started()
        return frozenset(self._tools_by_id)

    def list_servers(self) -> list[McpServerSnapshot]:
        self._require_started()
        return [
            McpServerSnapshot(
                server_id=server.server_id,
                transport=server.transport,
                enabled=server.enabled,
                status=(
                    "ready"
                    if server.enabled and server.server_id in self._clients
                    else "disabled"
                ),
                tools=self._descriptors_by_server.get(server.server_id, ()),
            )
            for server in self._servers
        ]

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("McpRuntimeManager 尚未启动")


def enabled_mcp_connections(
    raw_config: object,
    *,
    workspace_root: Path,
) -> dict[str, McpServerConfig]:
    return {
        server.server_id: server
        for server in parse_mcp_server_configs(
            raw_config,
            workspace_root=workspace_root,
        )
        if server.enabled
    }
