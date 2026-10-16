"""统一资源寻址纯值对象：语义资源描述、resolved handle、provenance 与解析上下文。

四种标识严格分离：display_uri 只是安全展示/引用（经 grammar 校验）；
resource_id/source_id 是 Registry 内稳定 identity，不得是 URI 或路径形态；
provider locator 只存在于实际 owner 私有侧，绝不进入本模块任何字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal, NewType
from uuid import UUID, uuid5

from app.core.resource_uri import (
    parse_resource_uri,
)

OPERATION_READ_CONTENT: Final = "read_content"
OPERATION_ACTIVATE: Final = "activate"
OPERATION_OBSERVE: Final = "observe"
ALL_OPERATIONS: Final = frozenset(
    {OPERATION_READ_CONTENT, OPERATION_ACTIVATE, OPERATION_OBSERVE}
)

# 描述符 kind 闭集（SemanticResourceDescriptor 侧）；与 grammar.py 的语法闭集
# _RESOURCE_KINDS 是两个独立闭集，不可用其一校验另一。本闭集只含实际被语义
# ResourceRegistry 登记为已发布语义描述符的 kind（agent-spec/skills）；语法闭集
# 内的 config/session 是纯寻址 kind（无 payload 描述符），不得登记进本闭集。
_DESCRIPTOR_KINDS: Final = frozenset({"agent-spec", "skills"})
_IDENTITY_FORBIDDEN: Final = ("/", "\\", "@", "%")
_RESOURCE_IDENTITY_NAMESPACE: Final = UUID("75e37b63-cb5f-5b6f-8c0d-c5d61d280ed3")
SESSION_RESOURCE_IDENTITY_NAMESPACE: Final = "session-resource-v1"
CONTEXT_ASSEMBLY_IDENTITY_NAMESPACE: Final = "context-assembly-v1"
ResourceIdentity = NewType("ResourceIdentity", str)

ResourceTargetAction = Literal[
    "reuse_reference", "retain_reference", "copy_snapshot", "omit", "unsupported"
]


@dataclass(frozen=True, slots=True)
class RuntimeResourceTargetFacts:
    """原运行资源 owner 的业务身份与准入事实；不是资源 URI 或第二份登记。"""

    kind: Literal["terminal", "browser"]
    resource_id: str
    workspace_id: str
    retention_scope: Literal["thread", "workspace"]
    source_thread: tuple[str, str]
    thread_associations: tuple[tuple[str, str], ...]
    owner_revision: int
    status: str
    reconciliation_status: str
    available_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResourceTargetFacts:
    """领域 owner 提供的冻结事实；不登记第二套资源正文或生命周期。"""

    resource_uri: str | None
    storage_domain_id: str
    lifecycle: Literal["workspace", "owner", "snapshot", "runtime"]
    available: bool
    required_for_context: bool = False
    snapshot_import_supported: bool = False
    owner_session_id: str | None = None
    owner_thread_id: str | None = None
    # owner 声明的保活范围，不等于 URI scope，也不授予正文读取权限。
    retention_scope: Literal["workspace", "session", "thread"] | None = None
    # 内容修订标识（附件为 blob_id），不是 catalog 的全局变更计数。
    revision: str | None = None
    # 带算法前缀的内容摘要，例如 sha256:<hex>。
    content_hash: str | None = None
    # 正文长度，单位 bytes；与内容修订/摘要共同证明目标相同内容。
    content_length: int | None = None
    runtime: RuntimeResourceTargetFacts | None = None


@dataclass(frozen=True, slots=True)
class ResourceTargetContext:
    storage_domain_id: str
    snapshot_import_supported: bool = True
    owner_session_id: str | None = None
    owner_thread_id: str | None = None
    existing_reference: ResourceTargetFacts | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceTargetDecision:
    resource_uri: str | None
    action: ResourceTargetAction
    available: bool
    reason: str | None = None
    target_uri: str | None = None
    runtime: RuntimeResourceTargetFacts | None = None


def _require_identity(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceIdentity {field_name} 必须是非空字符串")
    if any(marker in value for marker in _IDENTITY_FORBIDDEN):
        raise ValueError(
            f"ResourceIdentity {field_name} 不得携带路径/URI/credential 形态: {value!r}"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"ResourceIdentity {field_name} 不得携带控制字符: {value!r}")


def validate_resource_identity(value: str) -> ResourceIdentity:
    """校验不透明资源身份并返回其唯一的 nominal 类型。"""
    _require_identity("resource_identity", value)
    return ResourceIdentity(value)


def derive_resource_identity(namespace: str, *stable_parts: str) -> ResourceIdentity:
    """从 owner 已持久化的稳定 ID 派生跨进程一致的 opaque UUID。"""
    if not namespace or not stable_parts or any(not part for part in stable_parts):
        raise ValueError("ResourceIdentity 派生参数必须是非空 namespace 和 ID")
    seed = json.dumps(
        [namespace, *stable_parts],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return validate_resource_identity(str(uuid5(_RESOURCE_IDENTITY_NAMESPACE, seed)))


def _require_revision(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"资源 revision {field_name} 必须是非空字符串")


def _require_opaque_ref(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Registry 私有引用 {field_name} 必须是非空字符串")
    if any(marker in value for marker in _IDENTITY_FORBIDDEN):
        raise ValueError(
            f"Registry 私有引用 {field_name} 不得是路径/URI/credential 形态: {value!r}"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"Registry 私有引用 {field_name} 不得携带控制字符: {value!r}")


@dataclass(frozen=True, slots=True)
class SemanticResourceDescriptor:
    """virtual_resources 的 URI 解析 catalog 描述，由 resolver 消费注入值。

    CatalogBinding 将它交给 ResolvedResourceHandle 与 ResourceProvenance；
    与 derivation.types 中的派生与登记描述不可互换。kind 必须与 URI.kind 匹配。
    semantic_revision/semantic_hash 来自注入的 catalog 描述，本类型仅验证二者非空，
    不自行计算，也不要求二者相等。

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
        parsed = parse_resource_uri(self.display_uri)
        if parsed.kind != self.kind:
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
        parse_resource_uri(self.display_uri)
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
    # ``user`` scope 的 principal：单用户本地程序的约定值（见
    # ``app/core/user_identity.py``）。它是固定契约值而非部署相关量，但仍在 resolve 时
    # 由调用方显式携带并参与绑定校验（与 workspace/gateway/inline 同纪律），
    # MUST NOT 让 `user` 成为「parse 成功而 resolve 无绑定」的悬空 scope。
    user_scope_id: str | None = None
    gateway_id: str | None = None
    distribution_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id",
            "user_scope_id",
            "gateway_id",
            "distribution_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    f"ResolutionContext.{field_name} 必须是非空字符串或 None"
                )
