"""从唯一扩展目录派生有界导航及用户选择的完整定义；不维护第二目录。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agents.policy.tool_capability import ToolCapabilityPolicy
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.mcp.tool_definitions import ToolDefinition

MAX_NAVIGATION_GROUPS = 8
MAX_NAVIGATION_LABEL_LENGTH = 80
MAX_NAVIGATION_DESCRIPTION_LENGTH = 120
_CONTROL_CHARACTERS = str.maketrans(dict.fromkeys((*range(0x20), 0x7F), " "))


def _label(value: str, limit: int) -> str:
    return value.translate(_CONTROL_CHARACTERS).strip()[:limit]


def extension_tool_guidance(
    definitions: Sequence[ToolDefinition],
    policies: Mapping[str, ToolCapabilityPolicy],
) -> str:
    """软隐藏只留分组导航；硬隐藏和执行禁用不投递名称、描述或契约。

    自动完整定义不截断，用户选择的目标与搜索共用冻结 facet 正文。
    外部描述仅作为 tail_only user 来源数据，不能提升为系统指令。
    """
    if set(policies) != {definition.metadata.tool_id for definition in definitions}:
        raise ValueError("扩展导航需要同一次选择快照的完整有效策略")
    discoverable = sorted((definition for definition in definitions
                           if policies[definition.metadata.tool_id].execution_enabled
                           and not policies[definition.metadata.tool_id].model_visibility_locked),
                          key=lambda definition: definition.metadata.tool_id)
    groups: dict[str, list[ToolDefinition]] = {}
    for definition in discoverable:
        groups.setdefault(definition.metadata.group_id, []).append(definition)
    navigation = [
        {"group": _label(group, MAX_NAVIGATION_LABEL_LENGTH), "tool_count": len(entries),
         "example_capability": _label(str(entries[0].payload["description"]), MAX_NAVIGATION_DESCRIPTION_LENGTH)}
        for group, entries in sorted(groups.items())[:MAX_NAVIGATION_GROUPS]
    ]
    automatic = [
        {"tool_name": definition.metadata.tool_id, **definition.payload, "schema_hash": definition.schema_hash}
        for definition in discoverable if policies[definition.metadata.tool_id].model_visible
    ]
    return canonical_json_bytes({
        "discovery": "使用 search_tools(query=任务) 或 search_tools(tool_ids=[精确工具ID]) 获取完整定义；通过 invoke_extension_tool 调用。Skill 正文仍须由 skill_load 激活。",
        "navigation": navigation, "additional_groups": max(0, len(groups) - len(navigation)),
        "automatic_definitions": automatic,
    }).decode("utf-8")
