"""VRN 纯值对象：语义资源描述、resolved handle、provenance 与解析上下文。

四种标识严格分离：display_uri 只是安全展示/引用（经 grammar 校验）；
resource_id/source_id 是 Registry 内稳定 identity，不得是 URI 或路径形态；
provider locator 只存在于实际 owner 私有侧，绝不进入本模块任何字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    parse_vrn,
)

OPERATION_READ_CONTENT: Final = "read_content"
OPERATION_ACTIVATE: Final = "activate"
OPERATION_OBSERVE: Final = "observe"
ALL_OPERATIONS: Final = frozenset(
    {OPERATION_READ_CONTENT, OPERATION_ACTIVATE, OPERATION_OBSERVE}
)

_DESCRIPTOR_KINDS: Final = frozenset({"agent-spec", "skills", "memory"})
_IDENTITY_FORBIDDEN: Final = ("/", "\\", "@", "%")


def _require_identity(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"VRN {field_name} 必须是非空字符串")
    if any(marker in value for marker in _IDENTITY_FORBIDDEN):
        raise ValueError(
            f"VRN {field_name} 是内部稳定 identity，不得携带路径/URI/credential 形态: {value!r}"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"VRN {field_name} 不得携带控制字符: {value!r}")


def _require_revision(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"VRN {field_name} 必须是非空字符串")


def _require_opaque_ref(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"VRN {field_name} 必须是非空字符串")
    if any(marker in value for marker in _IDENTITY_FORBIDDEN):
        raise ValueError(
            f"VRN {field_name} 是 Registry 私有引用，不得是路径/URI/credential 形态: {value!r}"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"VRN {field_name} 不得携带控制字符: {value!r}")


@dataclass(frozen=True, slots=True)
class SemanticResourceDescriptor:
    """语义 Registry 内稳定的资源描述。

    resource_id 跨 rename/locator 变化保持不变；display_uri 只是当前
    逻辑地址，不得被当作 identity。字段集刻意不含 provider locator。
    """

    resource_id: str
    source_id: str
    kind: str
    display_uri: str
    semantic_revision: str
    semantic_hash: str

    def __post_init__(self) -> None:
        _require_identity("resource_id", self.resource_id)
        _require_identity("source_id", self.source_id)
        if self.kind not in _DESCRIPTOR_KINDS:
            raise ValueError(f"未知 SemanticResourceDescriptor.kind: {self.kind!r}")
        parsed = parse_vrn(self.display_uri)
        if self.kind == "memory":
            if parsed.scope != "memory":
                raise ValueError(
                    "kind=memory 的 display_uri 必须是 boxteam://memory/ URI"
                )
        elif parsed.scope == "memory" or parsed.kind != self.kind:
            raise ValueError(
                f"display_uri 与 kind 不一致: kind={self.kind!r} uri={self.display_uri!r}"
            )
        _require_revision("semantic_revision", self.semantic_revision)
        _require_revision("semantic_hash", self.semantic_hash)


@dataclass(frozen=True, slots=True)
class ResourceProvenance:
    """随 context/activation 封存的资源来源事实；不含任何 locator 字段。

    路径隐藏由构造保证：identity 字段拒绝路径/URI/credential 形态，
    display_uri 经 grammar 校验只可能是 boxteam:// 逻辑地址。
    """

    display_uri: str
    resource_id: str
    source_id: str
    semantic_revision: str
    semantic_hash: str

    def __post_init__(self) -> None:
        _require_identity("resource_id", self.resource_id)
        _require_identity("source_id", self.source_id)
        parse_vrn(self.display_uri)
        _require_revision("semantic_revision", self.semantic_revision)
        _require_revision("semantic_hash", self.semantic_hash)


@dataclass(frozen=True, slots=True)
class ResolvedResourceHandle:
    """resolver 产出的 typed handle；携带语义 revision 与封存 provenance。

    snapshot_ref 是 Registry 私有 CAS/快照引用，不是物理路径。
    """

    descriptor: SemanticResourceDescriptor
    snapshot_ref: str
    capabilities: frozenset[str]
    provenance: ResourceProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, SemanticResourceDescriptor):
            raise TypeError(
                "ResolvedResourceHandle.descriptor 必须是 SemanticResourceDescriptor"
            )
        _require_opaque_ref("snapshot_ref", self.snapshot_ref)
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("ResolvedResourceHandle.capabilities 必须是 frozenset")
        unknown = self.capabilities - ALL_OPERATIONS
        if unknown:
            raise ValueError(f"ResolvedResourceHandle.capabilities 未登记: {unknown!r}")
        if not isinstance(self.provenance, ResourceProvenance):
            raise TypeError(
                "ResolvedResourceHandle.provenance 必须是 ResourceProvenance"
            )
        expected = ResourceProvenance(
            display_uri=self.descriptor.display_uri,
            resource_id=self.descriptor.resource_id,
            source_id=self.descriptor.source_id,
            semantic_revision=self.descriptor.semantic_revision,
            semantic_hash=self.descriptor.semantic_hash,
        )
        if self.provenance != expected:
            raise ValueError(
                "ResolvedResourceHandle.provenance 必须与 descriptor 封存事实一致"
            )


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """resolve 时的 principal 绑定；字段为 None 表示该 scope 未绑定。"""

    workspace_id: str | None = None
    gateway_id: str | None = None
    distribution_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("workspace_id", "gateway_id", "distribution_id"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    f"ResolutionContext.{field_name} 必须是非空字符串或 None"
                )
