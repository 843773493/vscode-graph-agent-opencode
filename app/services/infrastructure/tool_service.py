from __future__ import annotations

from typing import Protocol

from app.schemas.public_v2.tool import (
    ToolDTO,
    ToolInvokeRequest,
    ToolInvokeResultDTO,
    ToolSelectionPatchRequest,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


class ToolCatalog(Protocol):
    def get_available_tools(self, agent_id: str = "default") -> list[dict]:
        ...


class ToolService:
    def __init__(
        self,
        *,
        tool_catalog: ToolCatalog,
        selection_store: ToolSelectionStore,
        test_supported_tools: set[str],
    ):
        self._tool_catalog = tool_catalog
        self._selection_store = selection_store
        self._test_supported_tools = set(test_supported_tools)

    async def list(self, agent_id: str = "default") -> list[ToolDTO]:
        tools = self._tool_catalog.get_available_tools(agent_id)
        disabled = self._selection_store.disabled_tools(agent_id)
        return [
            ToolDTO(
                tool_id=tool["id"],
                name=tool["name"],
                description=tool["description"],
                parameters=tool["parameters"],
                category=tool.get("category", "general"),
                group_id=tool.get("group_id", "default"),
                group_name=tool.get("group_name", "默认工具"),
                kind=tool.get("kind", "default"),
                enabled=tool["id"] not in disabled,
                test_supported=tool["id"] in self._test_supported_tools,
            )
            for tool in tools
        ]

    async def get(self, tool_id: str, agent_id: str = "default") -> ToolDTO:
        tools = {t.tool_id: t for t in await self.list(agent_id)}
        return tools[tool_id]

    async def update_selection(
        self,
        request: ToolSelectionPatchRequest,
    ) -> list[ToolDTO]:
        tools = await self.list(request.agent_id)
        available = {tool.tool_id for tool in tools}
        changes = {change.tool_id: change.enabled for change in request.changes}
        unknown = set(changes) - available
        if unknown:
            raise ValueError(f"包含后端不支持的工具: {', '.join(sorted(unknown))}")
        disabled = self._selection_store.apply_changes(
            agent_id=request.agent_id,
            changes=changes,
        )
        return [
            tool.model_copy(update={"enabled": tool.tool_id not in disabled})
            for tool in tools
            if tool.tool_id in changes
        ]

    async def invoke(self, tool_id: str, invoke_request: ToolInvokeRequest) -> ToolInvokeResultDTO:
        return ToolInvokeResultDTO(
            tool_id=tool_id,
            status="success",
            result=f"Tool {tool_id} executed successfully",
            parameters=invoke_request.parameters,
        )
