from __future__ import annotations

from typing import Any, Protocol

from app.agents.policy import (
    AGENT_COLLABORATION_TOOL_GROUP,
    DEFAULT_TOOL_GROUP,
    ParsedCustomToolSpec,
    catalog_group_for_tool,
    parse_custom_tool_specs,
)
from app.services.infrastructure.config_service import ConfigService


class RuntimeToolCatalog(Protocol):
    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]: ...


class ToolCatalogService:
    """把运行时工具与配置中的扩展工具映射为前端目录结构。"""

    def __init__(
        self,
        *,
        runtime_catalog: RuntimeToolCatalog,
        config_service: ConfigService,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._config_service = config_service

    def get_available_tools(self, agent_id: str = "default") -> list[dict[str, Any]]:
        definitions = [
            {
                **item,
                "group_id": item.get("group_id", DEFAULT_TOOL_GROUP.group_id),
                "group_name": item.get(
                    "group_name",
                    DEFAULT_TOOL_GROUP.group_name,
                ),
                "kind": item.get("kind", DEFAULT_TOOL_GROUP.kind),
            }
            for item in self._runtime_catalog.get_available_tools(agent_id)
            if item["id"] != "invoke_custom_tool"
        ]
        tool_config = self._config_service.get_agent_tool_config(agent_id)
        enabled_extension_names = (
            self._config_service.resolve_agent_tool_policy(
                agent_id
            ).enabled_extension_names
        )
        for spec in parse_custom_tool_specs(
            tool_config.get("custom", []),
            context=f"agent {agent_id} 的 tools.custom",
        ):
            definition = self._custom_definition(spec)
            if definition["id"] in enabled_extension_names:
                definitions.append(definition)
        return definitions

    @staticmethod
    def _custom_definition(spec: ParsedCustomToolSpec) -> dict[str, Any]:
        module_name = spec.factory_path.split(":", 1)[0].rsplit(".", 1)[-1]
        known_group = catalog_group_for_tool(spec.name)
        if known_group == AGENT_COLLABORATION_TOOL_GROUP:
            group_fields = known_group.as_catalog_fields()
        else:
            group_fields = {
                "group_id": f"extension:{module_name}",
                "group_name": f"扩展工具 · {module_name}",
                "kind": "extension",
            }
        return {
            "id": spec.name,
            "name": spec.name,
            "description": spec.description or f"工作区扩展工具 {spec.name}",
            "parameters": {},
            "category": group_fields["kind"],
            **group_fields,
        }
