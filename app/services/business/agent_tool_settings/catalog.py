from __future__ import annotations

from typing import Any, Protocol

from app.agents.policy import (
    DEBUGGING_TOOL_GROUP,
    DEFAULT_TOOL_GROUP,
    ParsedCustomToolSpec,
    parse_custom_tool_specs,
)
from app.agents.policy.tool_groups import catalog_group_for_custom_tool
from app.agents.tools.debugging import debugging_tool_input_json_schema
from app.services.infrastructure.config_service import ConfigService


class ToolCatalogReader(Protocol):
    """读取包含禁用工具的管理定义；执行与模型可见状态由设置服务投影。"""

    def list_tool_definitions(
        self, agent_id: str | None = None
    ) -> list[dict[str, Any]]: ...


class AgentToolCatalog:
    """合成运行时工具与配置扩展的管理定义，不按执行开关筛选。"""

    def __init__(
        self,
        *,
        runtime_catalog: ToolCatalogReader,
        config_service: ConfigService,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._config_service = config_service

    def list_tool_definitions(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        resolved_agent_id = self._config_service.validate_agent_id(agent_id)
        definitions = [
            {
                **item,
                "origin": item.get("origin", "builtin"),
                "availability": item.get("availability", "unknown") if item.get("origin", "builtin") != "builtin" else item.get("availability"),
                "group_id": item.get("group_id", DEFAULT_TOOL_GROUP.group_id),
                "group_name": item.get(
                    "group_name",
                    DEFAULT_TOOL_GROUP.group_name,
                ),
                "kind": item.get("kind", DEFAULT_TOOL_GROUP.kind),
            }
            for item in self._runtime_catalog.list_tool_definitions(resolved_agent_id)
            if item["id"] != "invoke_extension_tool"
        ]
        tool_config = self._config_service.get_agent_tool_config(resolved_agent_id)
        for spec in parse_custom_tool_specs(
            tool_config.get("custom", []),
            context=f"agent {resolved_agent_id} 的 tools.custom",
        ):
            definition = self._custom_definition(spec)
            definitions.append(definition)
        return definitions

    @staticmethod
    def _custom_definition(spec: ParsedCustomToolSpec) -> dict[str, Any]:
        group = catalog_group_for_custom_tool(spec)
        parameters = (
            debugging_tool_input_json_schema(spec.name)
            if group == DEBUGGING_TOOL_GROUP
            else {}
        )
        return {
            "id": spec.name,
            "name": spec.name,
            "origin": "custom",
            "availability": "unknown",
            "description": spec.description or f"工作区扩展工具 {spec.name}",
            "parameters": parameters,
            "category": group.kind,
            **group.as_catalog_fields(),
        }
