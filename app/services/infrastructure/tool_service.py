from __future__ import annotations

from typing import Protocol

from app.agents.policy import DEFAULT_TOOL_GROUP, validate_tool_dependencies
from app.schemas.public_v2.tool import (
    ToolDTO,
    ToolSelectionPatchRequest,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


class ToolNotFoundError(LookupError):
    """请求的工具不在指定 Agent 的工具目录中。"""


class ToolSelectionError(ValueError):
    """工具开关请求无法形成有效的 Agent 工具集合。"""


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
            self._build_tool_dto(tool, agent_id=agent_id, disabled=disabled)
            for tool in tools
        ]

    def _build_tool_dto(
        self,
        tool: dict,
        *,
        agent_id: str,
        disabled: set[str],
    ) -> ToolDTO:
        tool_id = tool["id"]
        kind = tool.get("kind", DEFAULT_TOOL_GROUP.kind)
        return ToolDTO(
            tool_id=tool_id,
            name=tool["name"],
            description=tool["description"],
            parameters=tool["parameters"],
            category=tool.get("category", "general"),
            group_id=tool.get("group_id", DEFAULT_TOOL_GROUP.group_id),
            group_name=tool.get("group_name", DEFAULT_TOOL_GROUP.group_name),
            kind=kind,
            execution_enabled=tool_id not in disabled,
            model_visible=(
                tool_id not in disabled
                and self._selection_store.model_visible(
                    agent_id,
                    tool_id=tool_id,
                    kind=kind,
                )
            ),
            test_supported=tool_id in self._test_supported_tools,
        )

    async def get(self, tool_id: str, agent_id: str = "default") -> ToolDTO:
        tools = {t.tool_id: t for t in await self.list(agent_id)}
        tool = tools.get(tool_id)
        if tool is None:
            raise ToolNotFoundError(
                f"Agent {agent_id!r} 不存在工具 {tool_id!r}"
            )
        return tool

    async def update_selection(
        self,
        request: ToolSelectionPatchRequest,
    ) -> list[ToolDTO]:
        tools = await self.list(request.agent_id)
        available = {tool.tool_id for tool in tools}
        changes = {
            change.tool_id: (change.execution_enabled, change.model_visible)
            for change in request.changes
        }
        unknown = set(changes) - available
        if unknown:
            raise ToolSelectionError(
                f"包含后端不支持的工具: {', '.join(sorted(unknown))}"
            )
        candidate_enabled = {
            tool.tool_id for tool in tools if tool.execution_enabled
        }
        for tool_id, (execution_enabled, model_visible) in changes.items():
            if not execution_enabled and model_visible:
                raise ToolSelectionError(
                    f"工具 {tool_id!r} 未启用执行能力时不能对模型可见"
                )
            if execution_enabled:
                candidate_enabled.add(tool_id)
            else:
                candidate_enabled.discard(tool_id)
        try:
            validate_tool_dependencies(
                candidate_enabled,
                context=f"Agent {request.agent_id!r} 的工具开关",
            )
        except ValueError as error:
            raise ToolSelectionError(str(error)) from error
        try:
            self._selection_store.apply_changes(
                agent_id=request.agent_id,
                changes=changes,
            )
        except ValueError as error:
            raise ToolSelectionError(str(error)) from error
        refreshed = {tool.tool_id: tool for tool in await self.list(request.agent_id)}
        return [
            refreshed[tool_id]
            for tool_id in changes
        ]
