from __future__ import annotations
from typing import Any, Dict, List

from app.schemas.tool import ToolDTO, ToolInvokeRequest
from app.services.agent_execution_service import AgentExecutionService


class ToolService:
    async def list(self) -> list[ToolDTO]:
        tools = AgentExecutionService.get_available_tools()
        return [
            ToolDTO(
                tool_id=tool["id"],
                name=tool["name"],
                description=tool["description"],
                parameters=tool["parameters"],
                category=tool.get("category", "general")
            )
            for tool in tools
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
