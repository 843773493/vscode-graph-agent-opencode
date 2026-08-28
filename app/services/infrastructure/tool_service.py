from __future__ import annotations

from typing import Protocol

from app.agents.policy import (
    DEFAULT_TOOL_GROUP,
    ToolMetadata,
    ToolPolicyResolver,
    validate_tool_dependencies,
)
from app.schemas.internal_v2.tool import (
    ToolDTO,
    ToolSelectionPatchRequest,
)
from app.services.infrastructure.config_service import ConfigService
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
        config_service: ConfigService,
        test_supported_tools: set[str],
    ):
        self._tool_catalog = tool_catalog
        self._selection_store = selection_store
        self._config_service = config_service
        self._test_supported_tools = set(test_supported_tools)

    async def list(self, agent_id: str = "default") -> list[ToolDTO]:
        tools = self._tool_catalog.get_available_tools(agent_id)
        resolver = self._config_service.get_tool_policy_resolver(agent_id)
        execution_overrides = self._selection_store.execution_overrides(agent_id)
        visibility_overrides = self._selection_store.model_visibility_overrides(
            agent_id
        )
        return [
            self._build_tool_dto(
                tool,
                resolver=resolver,
                execution_overrides=execution_overrides,
                visibility_overrides=visibility_overrides,
            )
            for tool in tools
        ]

    def _build_tool_dto(
        self,
        tool: dict,
        *,
        resolver: ToolPolicyResolver,
        execution_overrides: dict[str, bool],
        visibility_overrides: dict[str, bool],
    ) -> ToolDTO:
        tool_id = tool["id"]
        kind = tool.get("kind", DEFAULT_TOOL_GROUP.kind)
        group_id = tool.get("group_id", DEFAULT_TOOL_GROUP.group_id)
        policy = resolver.resolve(
            ToolMetadata(
                tool_id=tool_id,
                origin=tool.get("origin", "builtin"),
                kind=kind,
                group_id=group_id,
            ),
            execution_override=execution_overrides.get(tool_id),
            model_visibility_override=visibility_overrides.get(tool_id),
        )
        return ToolDTO(
            tool_id=tool_id,
            name=tool["name"],
            origin=tool.get("origin", "builtin"),
            description=tool["description"],
            parameters=tool["parameters"],
            category=tool.get("category", "general"),
            group_id=group_id,
            group_name=tool.get("group_name", DEFAULT_TOOL_GROUP.group_name),
            kind=kind,
            execution_enabled=policy.execution_enabled,
            model_visible=policy.model_visible,
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
        resolver = self._config_service.get_tool_policy_resolver(request.agent_id)
        execution_overrides = self._selection_store.execution_overrides(
            request.agent_id
        )
        catalog_tools = {
            tool["id"]: tool
            for tool in self._tool_catalog.get_available_tools(request.agent_id)
        }
        for tool_id, (execution_enabled, model_visible) in changes.items():
            tool = catalog_tools[tool_id]
            metadata = ToolMetadata(
                tool_id=tool_id,
                origin=tool.get("origin", "builtin"),
                kind=tool.get("kind", DEFAULT_TOOL_GROUP.kind),
                group_id=tool.get("group_id", DEFAULT_TOOL_GROUP.group_id),
            )
            static_policy = resolver.resolve(metadata)
            if static_policy.execution_locked:
                raise ToolSelectionError(f"工具 {tool_id!r} 被策略禁止执行")
            if model_visible and static_policy.model_visibility_locked:
                raise ToolSelectionError(f"工具 {tool_id!r} 被策略禁止对模型可见")
            execution_overrides[tool_id] = execution_enabled
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
