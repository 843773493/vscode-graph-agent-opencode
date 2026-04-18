from __future__ import annotations
from typing import Any

from app.schemas.tool import ToolDTO, ToolInvokeRequest


class ToolService:
    async def list(self) -> list[ToolDTO]:
        return [
            ToolDTO(
                tool_id="read_file",
                name="Read File",
                description="读取指定路径的文件内容",
                parameters={
                    "path": {"type": "string", "description": "文件路径", "required": True},
                    "offset": {"type": "integer", "description": "起始行号", "required": False},
                    "limit": {"type": "integer", "description": "读取行数", "required": False}
                },
                category="workspace"
            ),
            ToolDTO(
                tool_id="write_file",
                name="Write File",
                description="写入内容到指定路径的文件",
                parameters={
                    "path": {"type": "string", "description": "文件路径", "required": True},
                    "content": {"type": "string", "description": "文件内容", "required": True}
                },
                category="workspace"
            ),
            ToolDTO(
                tool_id="edit_file",
                name="Edit File",
                description="编辑文件中的指定内容",
                parameters={
                    "path": {"type": "string", "description": "文件路径", "required": True},
                    "old_string": {"type": "string", "description": "要替换的文本", "required": True},
                    "new_string": {"type": "string", "description": "新的文本", "required": True}
                },
                category="workspace"
            ),
            ToolDTO(
                tool_id="run_command",
                name="Run Command",
                description="在工作区执行Shell命令",
                parameters={
                    "command": {"type": "string", "description": "要执行的命令", "required": True},
                    "timeout": {"type": "integer", "description": "超时时间(毫秒)", "required": False}
                },
                category="execution"
            ),
            ToolDTO(
                tool_id="workspace_search",
                name="Workspace Search",
                description="在工作区搜索文件内容",
                parameters={
                    "pattern": {"type": "string", "description": "搜索模式", "required": True},
                    "include": {"type": "array", "description": "包含文件模式", "required": False}
                },
                category="workspace"
            ),
            ToolDTO(
                tool_id="web_search",
                name="Web Search",
                description="在互联网上搜索信息",
                parameters={
                    "query": {"type": "string", "description": "搜索查询", "required": True},
                    "num_results": {"type": "integer", "description": "结果数量", "required": False}
                },
                category="external"
            )
        ]

    async def get(self, tool_id: str) -> ToolDTO:
        tools = {t.tool_id: t for t in await self.list()}
        return tools[tool_id]

    async def invoke(self, tool_id: str, invoke_request: ToolInvokeRequest) -> dict[str, Any]:
        return {
            "tool_id": tool_id,
            "status": "success",
            "result": f"Tool {tool_id} executed successfully",
            "parameters": invoke_request.parameters
        }
