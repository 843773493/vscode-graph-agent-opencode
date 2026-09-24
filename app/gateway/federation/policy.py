"""``permissions.federation`` 的原子热发布策略快照。

内置默认值为 ``default_effect="allow"``、``rules=[]``、
``hardening_enabled=false``：已认证且登记在同一 hub 拓扑的主体默认可使用全部
核心 discovery/send/read/wait/reply/transit，不要求预先配置 allowlist。

策略只影响每个实际操作与披露点的鉴权结果，不进入模型上下文、ToolSet、
canonical item 或 sealed assembly；因此权限更新不改变已提交 wire prefix，
也不重启既有 channel。候选规范化失败时明确报错且不部分生效。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Capability = Literal["discovery", "send", "read", "wait", "reply", "transit"]
Effect = Literal["allow", "deny"]

CORE_CAPABILITIES: tuple[Capability, ...] = (
    "discovery",
    "send",
    "read",
    "wait",
    "reply",
    "transit",
)


@dataclass(frozen=True, slots=True)
class FederationPolicyRule:
    """一条显式限制规则：命中即按 ``effect`` 处置。"""

    capability: Capability
    effect: Effect
    origin_gateway_id: str | None = None
    principal_ref: str | None = None

    def matches(
        self,
        *,
        capability: Capability,
        origin_gateway_id: str,
        principal_ref: str,
    ) -> bool:
        if self.capability != capability:
            return False
        if self.origin_gateway_id is not None and (
            self.origin_gateway_id != origin_gateway_id
        ):
            return False
        return not (
            self.principal_ref is not None and self.principal_ref != principal_ref
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "effect": self.effect,
            "origin_gateway_id": self.origin_gateway_id,
            "principal_ref": self.principal_ref,
        }


@dataclass(frozen=True, slots=True)
class FederationPolicySnapshot:
    """不可变策略快照：``revision``/``content_hash`` 是审计与诊断身份。"""

    revision: int
    default_effect: Effect
    rules: tuple[FederationPolicyRule, ...]
    hardening_enabled: bool
    content_hash: str

    def evaluate(
        self,
        *,
        capability: Capability,
        origin_gateway_id: str,
        principal_ref: str,
    ) -> bool:
        """返回该实际操作/披露点是否获准；规则命中优先于默认值。"""

        effect = self.default_effect
        for rule in self.rules:
            if rule.matches(
                capability=capability,
                origin_gateway_id=origin_gateway_id,
                principal_ref=principal_ref,
            ):
                effect = rule.effect
        return effect == "allow"


def _normalize_rule(raw: Mapping[str, object]) -> FederationPolicyRule:
    capability = raw.get("capability")
    if capability not in CORE_CAPABILITIES:
        raise ValueError(f"federation 规则 capability 非法: {capability!r}")
    effect = raw.get("effect", "deny")
    if effect not in ("allow", "deny"):
        raise ValueError(f"federation 规则 effect 非法: {effect!r}")
    origin = raw.get("origin_gateway_id")
    principal = raw.get("principal_ref")
    for name, value in (("origin_gateway_id", origin), ("principal_ref", principal)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"federation 规则 {name} 必须是非空字符串或 null")
    return FederationPolicyRule(
        capability=capability,
        effect=effect,
        origin_gateway_id=origin,
        principal_ref=principal,
    )


def _canonical_bytes(value: object) -> bytes:
    """确定性序列化：键排序、无空白，供 content hash 使用。"""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def normalize_policy(
    raw: Mapping[str, object] | None,
) -> FederationPolicySnapshot:
    """把候选配置规范化为不可变快照；任何非法项都响亮拒绝且不部分生效。"""

    payload = dict(raw or {})
    default_effect = payload.get("default_effect", "allow")
    if default_effect not in ("allow", "deny"):
        raise ValueError(f"permissions.federation.default_effect 非法: {default_effect!r}")
    hardening = payload.get("hardening_enabled", False)
    if not isinstance(hardening, bool):
        raise TypeError("permissions.federation.hardening_enabled 必须是布尔值")
    raw_rules = payload.get("rules", [])
    if not isinstance(raw_rules, Iterable) or isinstance(raw_rules, (str, bytes)):
        raise TypeError("permissions.federation.rules 必须是数组")
    rules: list[FederationPolicyRule] = []
    for item in raw_rules:
        if not isinstance(item, Mapping):
            raise TypeError("permissions.federation.rules 元素必须是对象")
        rules.append(_normalize_rule(item))
    canonical = _canonical_bytes(
        {
            "default_effect": default_effect,
            "rules": [rule.to_payload() for rule in rules],
            "hardening_enabled": hardening,
        }
    )
    return FederationPolicySnapshot(
        revision=0,
        default_effect=default_effect,
        rules=tuple(rules),
        hardening_enabled=hardening,
        content_hash=hashlib.sha256(canonical).hexdigest(),
    )


class FederationPolicyStore:
    """持有当前策略快照并按原子热发布推进 revision。

    已有 channel 不重启；每个实际操作/披露点都读取最新 revision，所以撤权
    立即阻止尚未 durable acceptance 的操作，恢复后的下一次实际调用立即生效。
    """

    def __init__(self, *, initial: Mapping[str, object] | None = None) -> None:
        self._snapshot = normalize_policy(initial)

    @property
    def snapshot(self) -> FederationPolicySnapshot:
        return self._snapshot

    def publish(self, raw: Mapping[str, object]) -> FederationPolicySnapshot:
        """规范化候选并原子替换；失败时保留原快照，不做部分生效。"""

        candidate = normalize_policy(raw)
        self._snapshot = FederationPolicySnapshot(
            revision=self._snapshot.revision + 1,
            default_effect=candidate.default_effect,
            rules=candidate.rules,
            hardening_enabled=candidate.hardening_enabled,
            content_hash=candidate.content_hash,
        )
        return self._snapshot


def default_policy_payload() -> dict[str, object]:
    return {
        "default_effect": "allow",
        "rules": [],
        "hardening_enabled": False,
    }


__all__ = [
    "CORE_CAPABILITIES",
    "Capability",
    "Effect",
    "FederationPolicyRule",
    "FederationPolicySnapshot",
    "FederationPolicyStore",
    "default_policy_payload",
    "normalize_policy",
]
