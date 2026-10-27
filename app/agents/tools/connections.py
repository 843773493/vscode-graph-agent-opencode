"""本地扩展控制入口；所有连接与授权状态只消费工作区连接 service。"""

from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.custom_tools import CustomToolFactoryContext
from app.schemas.internal_v2.mcp import McpConnectionRequestDTO


class _ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ListConnections(_ConnectionInput):
    operation: Literal["list"]
    server_id: str | None = Field(default=None, min_length=1, max_length=64)


class ConnectServer(_ConnectionInput):
    operation: Literal["connect"]
    server_id: str = Field(min_length=1, max_length=64)


class DisconnectServer(_ConnectionInput):
    operation: Literal["disconnect"]
    server_id: str = Field(min_length=1, max_length=64)


class ConnectionStatus(_ConnectionInput):
    operation: Literal["status"]
    operation_id: str = Field(min_length=1, max_length=128)
    wait_timeout_ms: int = Field(default=0, ge=0, le=5000)


ConnectionRequest = Annotated[ListConnections | ConnectServer | DisconnectServer | ConnectionStatus, Field(discriminator="operation")]


class ConnectionsInput(_ConnectionInput):
    request: ConnectionRequest


def create_connections_tool(context: CustomToolFactoryContext) -> BaseTool:
    service = context.mcp_connection_service
    if service is None:
        raise RuntimeError("connections.manage 必须注入实际工作区的连接 service")

    async def manage(request: ConnectionRequest) -> dict[str, object]:
        try:
            if isinstance(request, ListConnections):
                return {"authorization_layer": "server", "connections": service.list_connections(request.server_id)}
            if isinstance(request, ConnectionStatus):
                operation = await service.status(request.operation_id, wait_timeout_ms=request.wait_timeout_ms)
            else:
                operation = await service.submit(McpConnectionRequestDTO(
                    operation_id=context.invocation_context.require_operation_id(f"mcp_{request.operation}"),
                    server_id=request.server_id, action=request.operation,
                ))
            # TODO(5.6/8.1)：授权 UI 需消费该精确工作区／操作入口；此处不伪造页面。
            return {"authorization_layer": "server", "operation": operation.model_dump(mode="json"),
                    "management_path": service.operation_path(operation)}
        except (LookupError, PermissionError, ValueError) as error:
            raise ToolException(str(error)) from error

    return StructuredTool.from_function(
        coroutine=manage, name="connections.manage", args_schema=ConnectionsInput,
        description="查询当前工作区已配置的 MCP 连接，发起连接、查询真实操作状态或断开。授权由用户通过产品入口完成；本工具不管理上游应用，不新增 Server 或返回凭据。",
        handle_tool_error=True,
    )
