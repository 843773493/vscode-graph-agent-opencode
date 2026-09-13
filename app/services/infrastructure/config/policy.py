from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConfigActivationPolicy = Literal[
    "immediate_read",
    "next_job",
    "next_session",
    "restart_workspace",
    "restart_gateway",
    "rejected",
]
ConfigActivationScope = Literal[
    "current",
    "next_job",
    "next_session",
    "restart_workspace",
    "restart_gateway",
]


@dataclass(frozen=True, slots=True)
class ConfigPolicyRule:
    """一个 JSON Pointer 策略；`*` 只匹配一个显式对象/数组层级。"""

    pattern: str
    policy: ConfigActivationPolicy
    activation_scope: ConfigActivationScope
    identity_key: str | None = None

    def matches(self, path: str) -> bool:
        pattern_parts = _pointer_parts(self.pattern)
        path_parts = _pointer_parts(path)
        if pattern_parts and pattern_parts[-1] == "*":
            prefix = pattern_parts[:-1]
            return len(path_parts) > len(prefix) and all(
                pattern_part == "*" or pattern_part == path_part
                for pattern_part, path_part in zip(prefix, path_parts, strict=False)
            )
        return len(pattern_parts) == len(path_parts) and all(
            pattern_part == "*" or pattern_part == path_part
            for pattern_part, path_part in zip(pattern_parts, path_parts, strict=True)
        )


@dataclass(frozen=True, slots=True)
class ConfigPolicyDecision:
    path: str
    policy: ConfigActivationPolicy
    activation_scope: ConfigActivationScope | Literal["mixed", "unknown"]
    rule_pattern: str | None
    policy_missing: bool


class ConfigPolicyRegistry:
    def __init__(
        self,
        *,
        domain: Literal["workspace", "gateway"],
        default_policy: Literal["restart_workspace", "restart_gateway"],
        rules: tuple[ConfigPolicyRule, ...],
    ) -> None:
        self.domain = domain
        self.default_policy = default_policy
        seen: set[str] = set()
        for rule in rules:
            if not rule.pattern.startswith("/"):
                raise ValueError(f"配置策略必须是 JSON Pointer: {rule.pattern}")
            if rule.pattern in seen:
                raise ValueError(f"配置策略重复登记: {rule.pattern}")
            seen.add(rule.pattern)
        for rule in rules:
            _pointer_parts(rule.pattern)
            if rule.activation_scope not in {
                "current",
                "next_job",
                "next_session",
                "restart_workspace",
                "restart_gateway",
            }:
                raise ValueError(
                    f"配置策略 activation_scope 无效: {rule.pattern}={rule.activation_scope}"
                )
            if rule.policy == "restart_gateway" and domain != "gateway":
                raise ValueError("Workspace 策略不能声明 restart_gateway")
            if rule.policy == "restart_workspace" and domain != "workspace":
                raise ValueError("Gateway 策略不能声明 restart_workspace")
            if (
                rule.policy != "rejected"
                and rule.activation_scope != rule.policy
                and not (
                    rule.policy == "immediate_read"
                    and rule.activation_scope == "current"
                )
            ):
                raise ValueError(
                    "配置策略 policy 与 activation_scope 不一致: "
                    f"{rule.pattern}={rule.policy}/{rule.activation_scope}"
                )
            if rule.identity_key is not None and not rule.pattern.endswith("/*"):
                raise ValueError(
                    f"数组 identity key 只能登记在对象通配符路径: {rule.pattern}"
                )
        for index, left in enumerate(rules):
            for right in rules[index + 1 :]:
                if _patterns_overlap(left.pattern, right.pattern):
                    raise ValueError(
                        "同优先级配置策略重叠: "
                        f"{left.pattern} 与 {right.pattern}"
                    )
        self.rules = rules

    def classify(self, paths: tuple[str, ...]) -> tuple[ConfigPolicyDecision, ...]:
        decisions: list[ConfigPolicyDecision] = []
        for path in paths:
            matches = [rule for rule in self.rules if rule.matches(path)]
            rule = max(
                matches,
                key=lambda item: len(_pointer_parts(item.pattern)),
                default=None,
            )
            if rule is None:
                decisions.append(
                    ConfigPolicyDecision(
                        path=path,
                        policy=self.default_policy,
                        activation_scope=self.default_policy,
                        rule_pattern=None,
                        policy_missing=True,
                    )
                )
            else:
                decisions.append(
                    ConfigPolicyDecision(
                        path=path,
                        policy=rule.policy,
                        activation_scope=rule.activation_scope,
                        rule_pattern=rule.pattern,
                        policy_missing=False,
                    )
                )
        return tuple(decisions)

    def restart_paths(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        restart_policy = (
            "restart_workspace" if self.domain == "workspace" else "restart_gateway"
        )
        return tuple(
            decision.path
            for decision in self.classify(paths)
            if decision.policy == restart_policy
        )

    def activation_scope_for(self, paths: tuple[str, ...]) -> str:
        """返回整个候选的生效范围；混合范围必须显式暴露给调用方。"""

        decisions = self.classify(paths)
        if not decisions:
            return "unknown"
        restart_policy = (
            "restart_workspace" if self.domain == "workspace" else "restart_gateway"
        )
        if any(decision.policy == restart_policy for decision in decisions):
            return restart_policy
        scopes = {decision.activation_scope for decision in decisions}
        return next(iter(scopes)) if len(scopes) == 1 else "mixed"

    def array_identity_keys(self) -> dict[str, str]:
        """导出策略声明的数组 identity key，供候选 diff 使用。"""

        return {
            rule.pattern.removesuffix("/*"): rule.identity_key
            for rule in self.rules
            if rule.identity_key is not None and rule.pattern.endswith("/*")
        }

    def policy_manifest(self) -> tuple[dict[str, object], ...]:
        """导出策略事实源，供 schema 注释、诊断和契约测试校验。"""

        return tuple(
            {
                "pattern": rule.pattern,
                "policy": rule.policy,
                "activation_scope": rule.activation_scope,
                "identity_key": rule.identity_key,
            }
            for rule in self.rules
        )


def validate_policy_manifest(
    schema: dict[str, object],
    registry: ConfigPolicyRegistry,
) -> None:
    """校验 schema 中声明的策略事实源没有脱离运行时策略表。"""

    manifest = schema.get("x-boxteam-policy-manifest")
    if manifest is None:
        return
    if not isinstance(manifest, list):
        raise TypeError("schema 的 x-boxteam-policy-manifest 必须是数组")
    expected = list(registry.policy_manifest())
    if manifest != expected:
        raise ValueError(
            "schema 的 x-boxteam-policy-manifest 与运行时策略表不一致: "
            f"expected={expected!r}, actual={manifest!r}"
        )


def _pointer_parts(path: str) -> tuple[str, ...]:
    if path == "/":
        return ()
    if not path.startswith("/"):
        raise ValueError(f"配置路径不是 JSON Pointer: {path}")
    return tuple(
        item.replace("~1", "/").replace("~0", "~")
        for item in path[1:].split("/")
    )


def _patterns_overlap(left: str, right: str) -> bool:
    left_parts = _pointer_parts(left)
    right_parts = _pointer_parts(right)
    if len(left_parts) != len(right_parts):
        return False
    return all(
        left_part == right_part or left_part == "*" or right_part == "*"
        for left_part, right_part in zip(left_parts, right_parts, strict=True)
    )


def workspace_config_policy() -> ConfigPolicyRegistry:
    return ConfigPolicyRegistry(
        domain="workspace",
        default_policy="restart_workspace",
        rules=(
            ConfigPolicyRule("/ui/*", "next_session", "next_session"),
            ConfigPolicyRule("/llm/providers", "next_job", "next_job"),
            ConfigPolicyRule("/runtime/agent/run/timeout_seconds", "next_job", "next_job"),
            ConfigPolicyRule("/runtime/agent/run/mode", "next_session", "next_session"),
            ConfigPolicyRule("/runtime/auxiliary_services/*", "next_job", "next_job"),
            ConfigPolicyRule("/runtime/gateway/connection/*", "next_job", "next_job"),
            ConfigPolicyRule("/mcp", "restart_workspace", "restart_workspace"),
            ConfigPolicyRule("/logger", "restart_workspace", "restart_workspace"),
        ),
    )


def gateway_config_policy() -> ConfigPolicyRegistry:
    return ConfigPolicyRegistry(
        domain="gateway",
        default_policy="restart_gateway",
        rules=(
            ConfigPolicyRule("/ui/*", "immediate_read", "current"),
            ConfigPolicyRule("/features/session_catalog/*", "next_job", "next_job"),
            ConfigPolicyRule("/features/session_generators/*", "next_job", "next_job"),
            ConfigPolicyRule("/runtime/gateway/process/*", "restart_gateway", "restart_gateway"),
            ConfigPolicyRule("/workspaces", "restart_gateway", "restart_gateway"),
            ConfigPolicyRule(
                "/workspaces/*",
                "restart_gateway",
                "restart_gateway",
                identity_key="connection_id",
            ),
        ),
    )
