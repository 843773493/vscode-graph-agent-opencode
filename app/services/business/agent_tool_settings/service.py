from __future__ import annotations

import asyncio
from typing import Any

from app.agents.policy import (
    DEFAULT_TOOL_GROUP,
    ToolMetadata,
    ToolPolicyResolver,
    validate_tool_dependencies,
)
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.tool import (
    ToolDTO,
    ToolSelectionPatchRequest,
)
from app.services.business.agent_tool_settings.catalog import ToolCatalogReader
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


class ToolNotFoundError(LookupError):
    """请求的工具不在指定 Agent 的工具目录中。"""


class ToolSelectionError(ValueError):
    """工具开关请求无法形成有效的 Agent 工具集合。"""


class AgentToolSettingsService:
    """查询和修改工作区内 Agent 的工具执行、模型可见开关。"""

    def __init__(
        self,
        *,
        tool_catalog: ToolCatalogReader,
        selection_store: ToolSelectionStore,
        config_service: ConfigService,
        test_supported_tools: set[str],
    ):
        self._tool_catalog = tool_catalog
        self._selection_store = selection_store
        self._config_service = config_service
        self._test_supported_tools = set(test_supported_tools)
        self._lock = asyncio.Lock()

    async def list(self, agent_id: str | None = None) -> list[ToolDTO]:
        async with self._lock:
            return await run_sync_shielded(self._list, agent_id)

    def _list(self, agent_id: str | None) -> list[ToolDTO]:
        snapshot, definitions = self._tool_catalog.list_tool_definitions(agent_id)
        with self._config_service.use_snapshot(snapshot):
            agent_id = self._config_service.validate_agent_id(agent_id)
            return self._project_tools(agent_id, definitions)

    def _project_tools(self, agent_id: str, definitions: list[dict[str, Any]]) -> list[ToolDTO]:
        resolver = self._config_service.get_tool_policy_resolver(agent_id)
        selection = self._selection_store.snapshot(agent_id)
        execution_overrides = selection.execution_overrides
        visibility_overrides = selection.model_visibility_overrides
        return [
            self._build_tool_dto(
                tool, resolver=resolver,
                execution_overrides=execution_overrides,
                visibility_overrides=visibility_overrides,
            )
            for tool in definitions
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
            execution_locked=policy.execution_locked,
            model_visibility_locked=policy.model_visibility_locked,
            availability=tool.get("availability"),
            availability_detail=tool.get("availability_detail"),
        )

    async def get(self, tool_id: str, agent_id: str | None = None) -> ToolDTO:
        async with self._lock:
            return await run_sync_shielded(
                self._get,
                tool_id,
                agent_id,
            )

    def _get(self, tool_id: str, agent_id: str | None) -> ToolDTO:
        snapshot, definitions = self._tool_catalog.list_tool_definitions(agent_id)
        with self._config_service.use_snapshot(snapshot):
            resolved_agent_id = self._config_service.validate_agent_id(agent_id)
            definition = next((tool for tool in definitions if tool["id"] == tool_id), None)
            if definition is None:
                raise ToolNotFoundError(
                    f"Agent {resolved_agent_id!r} 不存在工具 {tool_id!r}"
                )
            return self._project_tools(resolved_agent_id, [definition])[0]

    async def update_selection(
        self,
        request: ToolSelectionPatchRequest,
    ) -> list[ToolDTO]:
        async with self._lock:
            return await run_sync_shielded(self._update_selection, request)

    def _update_selection(self, request: ToolSelectionPatchRequest) -> list[ToolDTO]:
        # 只将请求中的无效 Agent 映射为选择错误，不把 factory／schema 错误改成输入错误。
        with self._config_service.use_snapshot(self._config_service.get_active_snapshot()):
            try:
                self._config_service.validate_agent_id(request.agent_id)
            except ValueError as error:
                raise ToolSelectionError(str(error)) from error
        snapshot, definitions = self._tool_catalog.list_tool_definitions(request.agent_id)
        with self._config_service.use_snapshot(snapshot):
            agent_id = self._config_service.validate_agent_id(request.agent_id)
            current_tools = self._project_tools(agent_id, definitions)
            tools_by_id = {tool.tool_id: tool for tool in current_tools}
            changes = {
                change.tool_id: (change.execution_enabled, change.model_visible)
                for change in request.changes
            }
            unknown = set(changes) - tools_by_id.keys()
            if unknown:
                raise ToolSelectionError(
                    f"包含后端不支持的工具: {', '.join(sorted(unknown))}"
                )
            for tool_id, (execution_enabled, model_visible) in changes.items():
                tool = tools_by_id[tool_id]
                if tool.execution_locked:
                    raise ToolSelectionError(f"工具 {tool_id!r} 被策略禁止执行")
                if model_visible and tool.model_visibility_locked:
                    raise ToolSelectionError(f"工具 {tool_id!r} 被策略禁止对模型可见")
            candidate_enabled = {
                tool.tool_id for tool in current_tools if tool.execution_enabled
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
                    context=f"Agent {agent_id!r} 的工具开关",
                )
            except ValueError as error:
                raise ToolSelectionError(str(error)) from error
            # 仅提交持已有发布锁；目录构造始终在锁外，避免与 catalog 构造锁倒置。
            with self._config_service.publication_lock:
                if self._config_service.get_active_snapshot().revision != snapshot.revision:
                    raise ToolSelectionError("工具配置已更新，请重新读取后重试")
                try:
                    self._selection_store.apply_changes(agent_id=agent_id, changes=changes)
                except ValueError as error:
                    raise ToolSelectionError(str(error)) from error
                refreshed = {tool.tool_id: tool for tool in self._project_tools(agent_id, [
                    definition for definition in definitions if definition["id"] in changes
                ])}
            return [refreshed[tool_id] for tool_id in changes]
