"""资源激活边界的不可变合同和唯一持久化 port。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.derivation.types import (
        ResourceSnapshot,
    )

ResourceActivationBoundary = Literal["turn", "model_call"]
_BOUNDARIES: frozenset[str] = frozenset({"turn", "model_call"})
_KIND_PATTERN_ERROR = "resource kind 必须匹配 ^[a-z][a-z0-9_-]{0,63}$"


def _require_nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value


@dataclass(frozen=True, slots=True)
class ResourceActivationPolicySnapshot:
    """一次 Turn 冻结的 activation policy；热更新只影响下一个 Turn。"""

    revision: str
    hash: str
    default_boundary: ResourceActivationBoundary
    boundaries: Mapping[str, ResourceActivationBoundary]

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> ResourceActivationPolicySnapshot:
        """从 Workspace 配置的 context.resource_activation 构造 policy。"""

        context = config.get("context")
        if not isinstance(context, Mapping):
            raise TypeError("context 配置必须是对象")
        section = context.get("resource_activation")
        if not isinstance(section, Mapping):
            raise TypeError("context.resource_activation 配置必须是对象")
        unknown_section_keys = set(section) - {"default_boundary", "overrides"}
        if unknown_section_keys:
            raise ValueError(
                f"context.resource_activation 存在未登记字段: {sorted(unknown_section_keys)}"
            )
        default_boundary = section.get("default_boundary")
        if default_boundary not in _BOUNDARIES:
            raise ValueError("context.resource_activation.default_boundary 必须是 turn|model_call")
        raw_overrides = section.get("overrides")
        if not isinstance(raw_overrides, Mapping):
            raise TypeError("context.resource_activation.overrides 必须是对象")
        boundaries: dict[str, ResourceActivationBoundary] = {}
        for key, value in raw_overrides.items():
            if (
                not isinstance(key, str)
                or len(key) == 0
                or len(key) > 64
                or key[0] not in "abcdefghijklmnopqrstuvwxyz"
                or any(not (char.isascii() and (char.islower() or char.isdigit()) or char in "_-") for char in key)
            ):
                raise ValueError(_KIND_PATTERN_ERROR)
            if value not in _BOUNDARIES:
                raise ValueError(
                    f"context.resource_activation.overrides.{key} 必须是 turn|model_call"
                )
            boundaries[key] = value  # type: ignore[assignment]
        normalized = {
            "default_boundary": default_boundary,
            "overrides": {key: boundaries[key] for key in sorted(boundaries)},
        }
        content_hash = sha256_jcs(normalized)
        return cls(
            revision=f"resource-activation-policy:v1:{content_hash.removeprefix('sha256:jcs:v1:')}",
            hash=content_hash,
            default_boundary=default_boundary,  # type: ignore[arg-type]
            boundaries={key: boundaries[key] for key in sorted(boundaries)},
        )

    def effective_boundary(self, resource_kind: str) -> ResourceActivationBoundary:
        """返回某个已登记 resource kind 的 effective boundary。"""

        return self.boundaries.get(resource_kind, self.default_boundary)

    def has_model_call_boundary(self) -> bool:
        return self.default_boundary == "model_call" or "model_call" in self.boundaries.values()


@dataclass(frozen=True, slots=True)
class ResourceActivationBinding:
    """一条已发布语义资源的冻结 binding；不携带 payload 或 locator。"""

    resource_id: str
    resource_kind: str
    facet: str
    display_uri: str
    semantic_revision: str
    content_length: int
    content_hash: str
    effective_boundary: ResourceActivationBoundary
    captured_registry_generation: int
    source_lineage: tuple[tuple[str, str], ...]
    source_lineage_digest: str = field(init=False, repr=False, compare=False)

    @classmethod
    def from_resource_snapshot(
        cls,
        snapshot: ResourceSnapshot,
        *,
        effective_boundary: ResourceActivationBoundary,
    ) -> ResourceActivationBinding:
        if not snapshot.available:
            raise ValueError(
                "不可用 ResourceSnapshot 不能进入 activation binding: "
                f"resource_id={snapshot.resource_id} error={snapshot.error}"
            )
        return cls(
            resource_id=snapshot.resource_id,
            resource_kind=snapshot.resource_kind,
            facet=snapshot.facet,
            display_uri=snapshot.display_uri,
            semantic_revision=snapshot.revision,
            content_length=len(canonical_json_bytes(snapshot.payload)),
            content_hash=sha256_jcs(snapshot.payload),
            effective_boundary=effective_boundary,
            captured_registry_generation=snapshot.generation,
            source_lineage=snapshot.source_lineage,
        )

    def __post_init__(self) -> None:
        for field_name in (
            "resource_id",
            "resource_kind",
            "facet",
            "display_uri",
            "semantic_revision",
            "content_hash",
        ):
            _require_nonempty(getattr(self, field_name), f"binding.{field_name}")
        if self.content_length < 0:
            raise ValueError("binding.content_length 必须是非负整数")
        if self.effective_boundary not in _BOUNDARIES:
            raise ValueError("binding.effective_boundary 必须是 turn|model_call")
        if self.captured_registry_generation < 0:
            raise ValueError("binding.captured_registry_generation 必须是非负整数")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
            for item in self.source_lineage
        ):
            raise ValueError("binding.source_lineage 必须是 (source_id, revision) 元组")
        object.__setattr__(
            self,
            "source_lineage_digest",
            sha256_jcs(tuple(sorted(self.source_lineage))),
        )


def _binding_selection(bindings: tuple[ResourceActivationBinding, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "resource_id": binding.resource_id,
            "resource_kind": binding.resource_kind,
            "facet": binding.facet,
            "display_uri": binding.display_uri,
            "semantic_revision": binding.semantic_revision,
            "content_length": binding.content_length,
            "content_hash": binding.content_hash,
            "availability": True,
        }
        for binding in bindings
    )


@dataclass(frozen=True, slots=True)
class TurnResourceSnapshot:
    """active execution slot 冻结的 Turn 级 binding 集。"""

    activation_snapshot_id: str
    snapshot_kind: Literal["turn"]
    policy: ResourceActivationPolicySnapshot
    owner_session_id: str
    owner_thread_id: str
    turn_id: str
    registry_generation: int
    bindings: tuple[ResourceActivationBinding, ...]
    bindings_hash: str = field(init=False, repr=False, compare=False)
    activation_provenance_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "activation_snapshot_id",
            "owner_session_id",
            "owner_thread_id",
            "turn_id",
        ):
            _require_nonempty(getattr(self, field_name), f"turn snapshot.{field_name}")
        if not isinstance(self.policy, ResourceActivationPolicySnapshot):
            raise TypeError("turn snapshot.policy 必须是 ResourceActivationPolicySnapshot")
        if not self.bindings:
            raise ValueError("turn snapshot 必须捕获至少一个 turn-bound binding")
        if any(binding.effective_boundary != "turn" for binding in self.bindings):
            raise ValueError("turn snapshot 只能包含 turn-bound binding")
        if len({binding.resource_id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("turn snapshot.binding resource_id 必须唯一")
        if self.registry_generation < 0:
            raise ValueError("turn snapshot.registry_generation 必须是非负整数")
        object.__setattr__(self, "bindings_hash", sha256_jcs(_binding_selection(self.bindings)))
        object.__setattr__(
            self,
            "activation_provenance_hash",
            sha256_jcs(
                {
                    "snapshot_kind": "turn",
                    "parent_snapshot_kind": None,
                    "activation_policy_revision": self.policy.revision,
                    "activation_policy_hash": self.policy.hash,
                    "registry_generation": self.registry_generation,
                    "bindings": tuple(
                        {
                            "effective_boundary": binding.effective_boundary,
                            "captured_registry_generation": binding.captured_registry_generation,
                            "source_lineage_digest": binding.source_lineage_digest,
                        }
                        for binding in self.bindings
                    ),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelCallResourceSnapshot:
    """tool protocol 收敛后安全 preparation 的 parent-linked snapshot。"""

    parent: TurnResourceSnapshot
    model_call_id: str
    registry_generation: int
    bindings: tuple[ResourceActivationBinding, ...]
    snapshot_kind: Literal["model_call"]
    bindings_hash: str = field(init=False, repr=False, compare=False)
    activation_provenance_hash: str = field(init=False, repr=False, compare=False)

    @property
    def parent_turn_snapshot_id(self) -> str:
        return self.parent.activation_snapshot_id

    def __post_init__(self) -> None:
        _require_nonempty(self.model_call_id, "model call snapshot.model_call_id")
        if not isinstance(self.parent, TurnResourceSnapshot):
            raise TypeError("model call snapshot.parent 必须是 TurnResourceSnapshot")
        if not self.bindings:
            raise ValueError("model call snapshot 必须复用 parent binding")
        parent_bindings = self.parent.bindings
        if self.bindings[: len(parent_bindings)] != parent_bindings:
            raise ValueError("model call snapshot 必须逐字节复用 parent turn-bound binding")
        if any(
            binding.effective_boundary == "turn"
            for binding in self.bindings[len(parent_bindings) :]
        ):
            raise ValueError("model call snapshot 追加 binding 不能是 turn-bound")
        if len({binding.resource_id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("model call snapshot.binding resource_id 必须唯一")
        if self.registry_generation < self.parent.registry_generation:
            raise ValueError("model call snapshot.registry_generation 不得小于 parent")
        object.__setattr__(self, "bindings_hash", sha256_jcs(_binding_selection(self.bindings)))
        object.__setattr__(
            self,
            "activation_provenance_hash",
            sha256_jcs(
                {
                    "snapshot_kind": "model_call",
                    "parent_snapshot_kind": "turn",
                    "activation_policy_revision": self.parent.policy.revision,
                    "activation_policy_hash": self.parent.policy.hash,
                    "registry_generation": self.registry_generation,
                    "bindings": tuple(
                        {
                            "effective_boundary": binding.effective_boundary,
                            "captured_registry_generation": binding.captured_registry_generation,
                            "source_lineage_digest": binding.source_lineage_digest,
                        }
                        for binding in self.bindings
                    ),
                }
            ),
        )


class ResourceActivationSnapshotSaver(Protocol):
    """itemized rollout owner 暴露给本目录的唯一持久化 port。"""

    async def save_resource_activation_snapshot(
        self,
        snapshot: TurnResourceSnapshot | ModelCallResourceSnapshot,
    ) -> None:
        """保存 typed snapshot；失败必须抛出，不允许 Coordinator 伪造成功。"""
        ...
