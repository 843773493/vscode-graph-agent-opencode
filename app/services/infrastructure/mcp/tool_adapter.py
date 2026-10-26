"""SDK 工具到 LangChain 的唯一薄适配，不占用任何公开参数名。"""

from __future__ import annotations

import asyncio

import anyio
import httpx
from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_audio_block
from langchain_core.tools import BaseTool, ToolException
from langchain_mcp_adapters.tools import _convert_mcp_content_to_lc_block

from app.agents.model_tool_schema import (
    CallBoundStructuredTool,
    model_tool_schema_validator,
)
from app.schemas.internal_v2.mcp import McpInvocationReceipt
from app.services.infrastructure.mcp.invocation import (
    McpCallState,
    confirm_mcp_result,
    mcp_call_scope,
)
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError


def _adapt_tool(session: ClientSession, definition: mcp_types.Tool) -> BaseTool:
    model_tool_schema_validator(definition.inputSchema)

    async def invoke(tool_call_id: str, tool_name: str, arguments: dict[str, object], /):
        # runtime 等字段也可能是合法的 MCP 参数，不能作为 Python 注入参数消费。
        state = McpCallState()
        progress_failure: Exception | None = None
        caller = asyncio.current_task()
        try:
            with mcp_call_scope(state) as progress_sink:
                async def report_progress(progress: float, total: float | None, message: str | None) -> None:
                    nonlocal progress_failure
                    if progress_sink is not None:
                        # Server 自由正文可能携带凭据或真实路径，只发布数值进度。
                        try:
                            await progress_sink(progress, total)
                        except Exception as error:  # noqa: BLE001 - SDK 会吞 callback 错误，必须交还原调用
                            # SDK 会仅记录 callback 异常；交回原调用 task，不能静默丢进度。
                            progress_failure = error
                            if caller is not None:
                                caller.cancel()

                result = await session.call_tool(definition.name, arguments, progress_callback=report_progress)
                confirm_mcp_result()
        except asyncio.CancelledError as error:
            receipt = McpInvocationReceipt(tool_name=tool_name, error_type=type(progress_failure or error).__name__,
                                           cancellation_delivery=state.cancellation_delivery)
            if progress_failure is not None:
                if not tool_call_id:
                    raise ToolException(receipt.model_dump_json()) from progress_failure
                return ToolMessage(content=receipt.model_dump_json(), tool_call_id=tool_call_id,
                                   name=tool_name, status="error", artifact=receipt.as_artifact()), None
            # asyncio.timeout 只识别原生 CancelledError，不能用子类破坏超时转换。
            raise asyncio.CancelledError(receipt) from error
        except (McpError, OSError, TimeoutError, httpx.TransportError,
                anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream) as error:
            if isinstance(error, McpError) and error.error.code not in {mcp_types.CONNECTION_CLOSED, 408}:
                confirm_mcp_result()
                raise
            receipt = McpInvocationReceipt(tool_name=tool_name, error_type=type(error).__name__,
                                           cancellation_delivery=state.cancellation_delivery)
            if not tool_call_id:
                raise ToolException(receipt.model_dump_json()) from error
            return ToolMessage(content=receipt.model_dump_json(), tool_call_id=tool_call_id,
                               name=tool_name, status="error", artifact=receipt.as_artifact()), None
        content: list[str | dict[str, object]] = [
            dict(create_audio_block(base64=block.data, mime_type=block.mimeType))
            if isinstance(block, mcp_types.AudioContent)
            else dict(_convert_mcp_content_to_lc_block(block))
            for block in result.content
        ]
        artifact = {"structured_content": result.structuredContent} if result.structuredContent is not None else None
        if tool_call_id:
            # isError 是真实业务结果；不能通过抛异常丢掉 structuredContent 或媒体。
            return ToolMessage(
                content=content, artifact=artifact, tool_call_id=tool_call_id,
                name=tool_name, status="error" if result.isError else "success",
            ), None
        if result.isError:
            raise ToolException(content)
        return content, artifact

    annotations = definition.annotations.model_dump() if definition.annotations is not None else {}
    metadata = {**annotations, **({"_meta": definition.meta} if definition.meta is not None else {})}
    return CallBoundStructuredTool(
        name=definition.name,
        description=definition.description or "",
        args_schema=definition.inputSchema,
        coroutine=invoke,
        response_format="content_and_artifact",
        metadata=metadata or None,
        handle_tool_error=lambda error: error.args[0],
    )


async def list_mcp_tools(session: ClientSession) -> list[BaseTool]:
    """完整读取后才返回；重复 cursor 和任何无效定义均使整次刷新失败。"""
    tools: list[BaseTool] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(_adapt_tool(session, definition) for definition in page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools
        if cursor in seen:
            raise ValueError("MCP tools/list 返回重复分页 cursor")
        seen.add(cursor)
