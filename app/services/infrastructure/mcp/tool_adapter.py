"""SDK 工具到 LangChain 的唯一薄适配，不占用任何公开参数名。"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.tools import (
    _convert_call_tool_result,
    _handle_mcp_tool_error,
)

from app.agents.model_tool_schema import model_tool_schema_validator
from mcp import types as mcp_types
from mcp.client.session import ClientSession


def _adapt_tool(session: ClientSession, definition: mcp_types.Tool) -> BaseTool:
    model_tool_schema_validator(definition.inputSchema)

    async def invoke(**arguments: object):
        # runtime 等字段也可能是合法的 MCP 参数，不能作为 Python 注入参数消费。
        return _convert_call_tool_result(await session.call_tool(definition.name, arguments))

    annotations = definition.annotations.model_dump() if definition.annotations is not None else {}
    metadata = {**annotations, **({"_meta": definition.meta} if definition.meta is not None else {})}
    return StructuredTool(
        name=definition.name,
        description=definition.description or "",
        args_schema=definition.inputSchema,
        coroutine=invoke,
        response_format="content_and_artifact",
        metadata=metadata or None,
        handle_tool_error=_handle_mcp_tool_error,  # type: ignore[arg-type] 上游转换器保留多模态错误正文。
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
