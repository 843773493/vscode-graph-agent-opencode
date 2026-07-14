from __future__ import annotations

from typing import Any, Protocol

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
                "group_id": "default",
                "group_name": "默认工具",
                "kind": "default",
            }
            for item in self._runtime_catalog.get_available_tools(agent_id)
            if item["id"] != "invoke_custom_tool"
        ]
        tool_config = self._config_service.get_agent_tool_config(agent_id)
        for spec in tool_config.get("custom", []):
            definitions.append(self._custom_definition(spec))
        return definitions

    @staticmethod
    def _custom_definition(spec: object) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise TypeError(f"tools.custom 条目必须是对象: {spec!r}")
        name = spec.get("name")
        factory = spec.get("factory")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tools.custom 条目缺少 name: {spec!r}")
        if not isinstance(factory, str) or ":" not in factory:
            raise ValueError(f"tools.custom 条目缺少有效 factory: {spec!r}")
        module_name = factory.split(":", 1)[0].rsplit(".", 1)[-1]
        return {
            "id": name,
            "name": name,
            "description": spec.get("description") or f"工作区扩展工具 {name}",
            "parameters": {},
            "category": "extension",
            "group_id": f"extension:{module_name}",
            "group_name": f"扩展工具 · {module_name}",
            "kind": "extension",
        }

