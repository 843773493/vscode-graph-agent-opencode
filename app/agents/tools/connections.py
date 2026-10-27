"""本地扩展控制入口；所有连接与授权状态只消费工作区连接 service。"""

from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from app.abstractions.mcp_connection_wait import McpWaitPayload, mcp_wait_task_id
from app.agents.custom_tools import CustomToolFactoryContext
from app.core.background_task_contracts import BackgroundTaskDescriptor
from app.core.execution_admission_context import require_current_execution_admission
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.internal_v2.mcp import McpConnectionRequestDTO


class _ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ListConnections(_ConnectionInput):
    operation: Literal["list"]
    server_id: str | None = Field(default=None, min_length=1, max_length=64)


class ConnectServer(_ConnectionInput):
    operation: Literal["connect"]
    server_id: str = Field(min_length=1, max_length=64)
    continuation: str | None = Field(default=None, min_length=1, max_length=4096, description="明确登记授权成功后继续的意图；省略时仅发起连接，不自动继续。")


class DisconnectServer(_ConnectionInput):
    operation: Literal["disconnect"]
    server_id: str = Field(min_length=1, max_length=64)


class ConnectionStatus(_ConnectionInput):
    operation: Literal["status"]
    operation_id: str = Field(min_length=1, max_length=128)
    wait_timeout_ms: int = Field(default=0, ge=0, le=5000)
    continuation: str | None = Field(default=None, min_length=1, max_length=4096, description="登记原 thread 的持久等待；成功后在原 Turn 结束后独立继续，不重放业务工具。")


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
            result: dict[str, object] = {
                "authorization_layer": "server", "operation": operation.model_dump(mode="json"),
                "management_path": service.operation_path(operation),
            }
            if isinstance(request, (ConnectServer, ConnectionStatus)) and request.continuation is not None:
                if operation.action != "connect":
                    raise ValueError("只有连接操作支持授权后的 continuation")
                binding = context.invocation_context.thread_binding
                if binding is None or binding.session_id != context.session_id:
                    raise RuntimeError("MCP 长等待缺少匹配的精确 thread binding")
                admission = require_current_execution_admission(langgraph_thread_id=binding.thread_id)
                task_id = mcp_wait_task_id(context.session_id, binding.thread_id, operation.operation_id)
                existing = context.background_task_registry.get_handle(context.session_id, task_id)
                if existing is not None:
                    previous = McpWaitPayload.model_validate(existing.descriptor.payload)
                    if previous.continuation != request.continuation:
                        raise ValueError("此 thread 已为同一连接操作登记不同 continuation")
                    handle = existing
                else:
                    payload = McpWaitPayload(
                        operation_id=operation.operation_id, connection_id=operation.connection_id,
                        deadline=operation.deadline, continuation=request.continuation,
                        model_call_id=context.invocation_context.require_model_call_id(),
                        tool_call_id=context.invocation_context.require_tool_call_id(),
                        origin_job_id=admission.job_id, origin_execution_id=admission.execution_id,
                        gateway_id=get_current_gateway_id(), request_id=get_current_request_id(),
                    )
                    handle = await context.background_task_registry.spawn(
                        session_id=context.session_id, thread_id=binding.thread_id, agent_id=context.agent_id,
                        task_id=task_id, descriptor=BackgroundTaskDescriptor(
                            kind="mcp_connection_wait", version=1, payload=payload.model_dump(mode="json"),
                        ),
                    )
                result["wait"] = {"task_id": handle.task_id, "status": handle.status, "thread_id": handle.thread_id,
                                  "deadline": operation.deadline, "delivery_policy": "after_turn"}
            return result
        except (LookupError, PermissionError, ValueError) as error:
            raise ToolException(str(error)) from error

    return StructuredTool.from_function(
        coroutine=manage, name="connections.manage", args_schema=ConnectionsInput,
        description="查询当前工作区已配置的 MCP 连接，发起连接、查询真实操作状态或断开。授权由用户通过产品入口完成；本工具不管理上游应用，不新增 Server 或返回凭据。",
        handle_tool_error=True,
    )
