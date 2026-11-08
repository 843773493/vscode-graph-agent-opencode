"""自有插件扩展点的版本、开放状态和原业务 owner 接缝。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.schemas.gateway.service_plugin import (
    ExtensionPoint,
    PluginExtensionRequirement,
)

InfrastructureStatus = Literal["present", "partial"]
BusinessStatus = Literal["connected", "partial"]
PluginStatus = Literal["open", "closed"]
AcceptanceStatus = Literal["pending", "accepted"]


@dataclass(frozen=True, slots=True)
class ExtensionPointSupport:
    id: ExtensionPoint
    version: int
    infrastructure: InfrastructureStatus
    business: BusinessStatus
    plugin: PluginStatus
    acceptance: AcceptanceStatus
    owner_seam: str


EXTENSION_POINTS: dict[str, ExtensionPointSupport] = {
    "commands": ExtensionPointSupport(
        "commands", 1, "present", "connected", "open", "pending",
        "Workbench Commands / Action2",
    ),
    "menus": ExtensionPointSupport(
        "menus", 1, "present", "connected", "open", "pending",
        "Workbench MenuRegistry / ContextKeyService",
    ),
    "keybindings": ExtensionPointSupport(
        "keybindings", 1, "present", "connected", "open", "pending",
        "Workbench KeybindingsRegistry / user keybinding resolver",
    ),
    "configuration": ExtensionPointSupport(
        "configuration", 1, "present", "partial", "open", "pending",
        "original Gateway and Workspace configuration owners",
    ),
    "services": ExtensionPointSupport(
        "services", 1, "present", "partial", "open", "pending",
        "Gateway plugin runtime and original MCP / Terminal / Browser owners",
    ),
    "debuggers": ExtensionPointSupport(
        "debuggers", 1, "present", "partial", "open", "pending",
        "Workbench Debug service and Backend node_debug owner",
    ),
    "skills": ExtensionPointSupport(
        "skills", 1, "present", "connected", "open", "pending",
        "Backend SkillCatalog and Gateway Skill sources",
    ),
}

# 首批没有获准关联的无服务工具；新增条目必须是明确的产品合同，不继承全部内置工具。
PRODUCT_APPROVED_SKILL_TOOL_IDS: frozenset[str] = frozenset()


def validate_extension_requirements(requirements: Iterable[PluginExtensionRequirement]) -> None:
    """拒绝未开放扩展点和版本；具体贡献仍归其原业务 owner 校验。"""
    for requirement in requirements:
        extension_point = requirement.extension_point
        version = requirement.version
        support = EXTENSION_POINTS.get(extension_point)
        if support is None or support.plugin != "open":
            raise ValueError(f"插件声明了未知或未开放的扩展点: {extension_point}")
        if version != support.version:
            raise ValueError(
                f"扩展点合同版本不兼容: {extension_point} required={version}, supported={support.version}"
            )


__all__ = [
    "EXTENSION_POINTS",
    "ExtensionPointSupport",
    "PRODUCT_APPROVED_SKILL_TOOL_IDS",
    "validate_extension_requirements",
]
