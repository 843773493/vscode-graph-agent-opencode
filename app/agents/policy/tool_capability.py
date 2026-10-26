from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from app.agents.policy.tool_groups import catalog_group_for_tool


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """策略匹配使用的稳定工具元数据；不包含展示文案。"""

    tool_id: str
    origin: str
    kind: str
    group_id: str


def metadata_for_tool(
    tool: BaseTool, *, origin: str, group_id: str | None = None,
) -> ToolMetadata:
    """目录、发现和执行共用的策略身份；展示描述不参与策略匹配。"""
    server_id = (tool.metadata or {}).get("mcp_server_id")
    if origin == "mcp" and isinstance(server_id, str) and server_id:
        return ToolMetadata(tool.name, origin, "extension", f"mcp:{server_id}")
    group = catalog_group_for_tool(tool.name)
    kind = group.kind if group.kind != "default" else ("default" if origin == "builtin" else "extension")
    return ToolMetadata(tool.name, origin, kind, group_id or group.group_id)


@dataclass(frozen=True, slots=True)
class ToolCapabilityPolicy:
    """单个工具合并后的有效能力和限制状态。"""

    execution_enabled: bool
    model_visible: bool
    confirmation_required: bool
    timeout_ms: int
    max_result_bytes: int
    execution_locked: bool
    model_visibility_locked: bool


_RULE_SCOPES = (
    ("by_origin", "origin"),
    ("by_kind", "kind"),
    ("by_group", "group_id"),
    ("by_tool", "tool_id"),
)


class ToolPolicyResolver:
    """统一解析 Workspace 静态策略和 Agent 局部策略。"""

    def __init__(
        self,
        *,
        policy_defaults: Mapping[str, object],
        policy_rules: Mapping[str, object],
        restrictions: Mapping[str, object],
    ) -> None:
        self._defaults = dict(policy_defaults)
        self._rules = dict(policy_rules)
        self._restrictions = dict(restrictions)

    def resolve(
        self,
        metadata: ToolMetadata,
        *,
        execution_override: bool | None = None,
        model_visibility_override: bool | None = None,
    ) -> ToolCapabilityPolicy:
        resolved_policy = {
            "execution_enabled": self._bool_value(
                self._defaults, "execution_enabled", True
            ),
            "model_visible": self._bool_value(
                self._defaults, "model_visible", True
            ),
            "confirmation_required": self._bool_value(
                self._defaults, "confirmation_required", False
            ),
            "timeout_ms": self._int_value(
                self._nested(self._defaults, "limits"), "timeout_ms", 10000
            ),
            "max_result_bytes": self._int_value(
                self._nested(self._defaults, "limits"),
                "max_result_bytes",
                1048576,
            ),
        }

        for scope_name, metadata_name in _RULE_SCOPES:
            scope_rules = self._mapping(self._rules.get(scope_name))
            patch = self._mapping(scope_rules.get(getattr(metadata, metadata_name)))
            self._apply_patch(resolved_policy, patch)

        metadata_selectors = {
            metadata.tool_id,
            f"tool:{metadata.tool_id}",
            metadata.group_id,
            f"group:{metadata.group_id}",
            metadata.kind,
            f"kind:{metadata.kind}",
            metadata.origin,
            f"origin:{metadata.origin}",
        }
        execution_locked = self._matches_restriction(
            "execution_disabled", metadata_selectors
        )
        model_visibility_locked = self._matches_restriction(
            "model_hidden", metadata_selectors
        )
        confirmation_restricted = self._matches_restriction(
            "confirmation_required", metadata_selectors
        )

        if execution_locked:
            resolved_policy["execution_enabled"] = False
        elif execution_override is not None:
            resolved_policy["execution_enabled"] = execution_override

        if model_visibility_locked:
            resolved_policy["model_visible"] = False
        elif model_visibility_override is not None:
            resolved_policy["model_visible"] = model_visibility_override

        if confirmation_restricted:
            resolved_policy["confirmation_required"] = True
        if not resolved_policy["execution_enabled"]:
            resolved_policy["model_visible"] = False

        return ToolCapabilityPolicy(
            execution_enabled=bool(resolved_policy["execution_enabled"]),
            model_visible=bool(resolved_policy["model_visible"]),
            confirmation_required=bool(resolved_policy["confirmation_required"]),
            timeout_ms=int(resolved_policy["timeout_ms"]),
            max_result_bytes=int(resolved_policy["max_result_bytes"]),
            execution_locked=execution_locked,
            model_visibility_locked=model_visibility_locked,
        )

    def _matches_restriction(
        self,
        restriction_name: str,
        metadata_selectors: set[str],
    ) -> bool:
        selectors = self._restrictions.get(restriction_name, [])
        if not isinstance(selectors, list):
            raise TypeError(f"工具策略 restrictions.{restriction_name} 必须是数组")
        return any(
            isinstance(selector, str) and selector in metadata_selectors
            for selector in selectors
        )

    @staticmethod
    def _apply_patch(
        resolved_policy: dict[str, object], patch: Mapping[str, object]
    ) -> None:
        for name in (
            "execution_enabled",
            "model_visible",
            "confirmation_required",
        ):
            policy_value = patch.get(name)
            if policy_value is not None:
                if not isinstance(policy_value, bool):
                    raise TypeError(f"工具策略 {name} 必须是布尔值")
                resolved_policy[name] = policy_value
        limits = patch.get("limits")
        if limits is not None:
            for name in ("timeout_ms", "max_result_bytes"):
                policy_value = ToolPolicyResolver._mapping(limits).get(name)
                if policy_value is not None:
                    if not isinstance(policy_value, int) or isinstance(policy_value, bool):
                        raise TypeError(f"工具策略 limits.{name} 必须是整数")
                    resolved_policy[name] = policy_value

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("工具策略规则必须是对象")
        return value

    @staticmethod
    def _nested(value: Mapping[str, object], name: str) -> Mapping[str, object]:
        return ToolPolicyResolver._mapping(value.get(name))

    @staticmethod
    def _bool_value(
        values: Mapping[str, object], name: str, default: bool
    ) -> bool:
        value = values.get(name, default)
        if not isinstance(value, bool):
            raise TypeError(f"工具策略 {name} 必须是布尔值")
        return value

    @staticmethod
    def _int_value(
        values: Mapping[str, object], name: str, default: int
    ) -> int:
        value = values.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"工具策略 {name} 必须是正整数")
        return value
