from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """策略匹配使用的稳定工具元数据；不包含展示文案。"""

    tool_id: str
    origin: str
    kind: str
    group_id: str


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
        values = {
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

        for rules in self._rule_layers():
            for scope_name, metadata_name in _RULE_SCOPES:
                scope_rules = self._mapping(rules.get(scope_name))
                patch = self._mapping(scope_rules.get(getattr(metadata, metadata_name)))
                self._apply_patch(values, patch)

        execution_locked = self._matches_restriction(
            "execution_disabled", metadata
        )
        model_visibility_locked = self._matches_restriction(
            "model_hidden", metadata
        )
        confirmation_restricted = self._matches_restriction(
            "confirmation_required", metadata
        )

        if execution_locked:
            values["execution_enabled"] = False
        elif execution_override is not None:
            values["execution_enabled"] = execution_override

        if model_visibility_locked:
            values["model_visible"] = False
        elif model_visibility_override is not None:
            values["model_visible"] = model_visibility_override

        if confirmation_restricted:
            values["confirmation_required"] = True
        if not values["execution_enabled"]:
            values["model_visible"] = False

        return ToolCapabilityPolicy(
            execution_enabled=bool(values["execution_enabled"]),
            model_visible=bool(values["model_visible"]),
            confirmation_required=bool(values["confirmation_required"]),
            timeout_ms=int(values["timeout_ms"]),
            max_result_bytes=int(values["max_result_bytes"]),
            execution_locked=execution_locked,
            model_visibility_locked=model_visibility_locked,
        )

    def _rule_layers(self) -> tuple[Mapping[str, object], ...]:
        return (self._rules,)

    def _matches_restriction(
        self,
        name: str,
        metadata: ToolMetadata,
    ) -> bool:
        selectors = self._restrictions.get(name, [])
        if not isinstance(selectors, list):
            raise TypeError(f"工具策略 restrictions.{name} 必须是数组")
        values = {
            metadata.tool_id,
            f"tool:{metadata.tool_id}",
            metadata.group_id,
            f"group:{metadata.group_id}",
            metadata.kind,
            f"kind:{metadata.kind}",
            metadata.origin,
            f"origin:{metadata.origin}",
        }
        return any(isinstance(selector, str) and selector in values for selector in selectors)

    @staticmethod
    def _apply_patch(values: dict[str, object], patch: Mapping[str, object]) -> None:
        for name in (
            "execution_enabled",
            "model_visible",
            "confirmation_required",
        ):
            value = patch.get(name)
            if value is not None:
                if not isinstance(value, bool):
                    raise TypeError(f"工具策略 {name} 必须是布尔值")
                values[name] = value
        limits = patch.get("limits")
        if limits is not None:
            for name in ("timeout_ms", "max_result_bytes"):
                value = ToolPolicyResolver._mapping(limits).get(name)
                if value is not None:
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise TypeError(f"工具策略 limits.{name} 必须是整数")
                    values[name] = value

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
