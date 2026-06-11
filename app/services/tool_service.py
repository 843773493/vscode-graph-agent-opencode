from __future__ import annotations
from typing import Protocol

from app.schemas.public_v2.tool import ToolDTO, ToolInvokeRequest, ToolInvokeResultDTO


class ToolCatalog(Protocol):
    def get_available_tools(self) -> list[dict]:
        ...


class ToolService:
    def __init__(self, *, tool_catalog: ToolCatalog):
        self._tool_catalog = tool_catalog
    
    async def list(self) -> list[ToolDTO]:
        tools = self._tool_catalog.get_available_tools()
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

    async def invoke(self, tool_id: str, invoke_request: ToolInvokeRequest) -> ToolInvokeResultDTO:
        return ToolInvokeResultDTO(
            tool_id=tool_id,
            status="success",
            result=f"Tool {tool_id} executed successfully",
            parameters=invoke_request.parameters,
        )
