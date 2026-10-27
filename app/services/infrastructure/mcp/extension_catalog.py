"""ExtensionCatalogBindingRef：model-call preparation 封存的扩展目录 binding。

OpenSpec add-context-injection-lifecycle E4：dispatcher/extension invoker 只按
sealed binding ref 解析旧 tool call；目录后续增删改/权限变化不影响已封存
ref，执行点按最新权限校验并返回真实 paired result。binding ref 是不可变、
可验证（binding_id 与 binding_hash 均可重算核对）的 typed value object。

两个 hash 的语义严格分离，且都复用 domain 的唯一 canonical 编码
（``app.domain.itemized.hashing.canonical_json_bytes`` + ``sha256_jcs``），
不自造第二套 JSON 规范化：

* ``binding_hash`` 只覆盖这条 sealed binding 自身的身份：binding_id、catalog
  semantic revision、server generation、Provider 信封 identity 与稳定
  target/schema 集合。它不随激活边界绑定的指引/策略变化。
* ``extension_dispatch_binding_hash`` 额外覆盖同一 ModelCallResourceSnapshot
  原子绑定的 MCP 指引 revision 与 activation policy revision，用于
  dispatch/restore 校验；它既不进入 ``context-plan-hash:v2``，也不进入仅
  描述 Provider wire bytes 的 ``request_hash``。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from app.domain.itemized.hashing import (
    ItemSchemaError,
    canonical_json_bytes,
    sha256_jcs,
)
from app.domain.itemized.resource_activation import ResourceMemberBinding

# 固定 Provider 信封的 binding identity。信封名称/参数形状变化必须产生新的
# provider_binding_identity，才允许进入 ToolSet hard rebase 合同（E3/E4 边界）。
EXTENSION_TOOL_ENVELOPE_IDENTITY = (
    "invoke_extension_tool@v1:tool_name:str+arguments:object"
)

_BINDING_ID_PREFIX = "ext-catalog:v3"
EXTENSION_CATALOG_BINDING_SCHEMA = "extension-catalog-binding:v3"
# 独立 dispatch binding hash 的版本化 JCS preimage schema；它覆盖 catalog
# semantic revision/hash、server generation 验证 ref、稳定 target/schema 集合、
# 绑定指引 revision 与 activation policy revision，与 binding_hash 分离。
EXTENSION_DISPATCH_BINDING_SCHEMA = "extension-dispatch-binding:v3"
# 历史 binding 丢失时的显式错误 code；调用方必须拒绝 dispatch，不得回退当前
# MCP 目录、当前同名 target 或空目录。
EXTENSION_CATALOG_UNAVAILABLE_CODE = "extension-catalog-unavailable"
EXTENSION_DISPATCH_BINDING_MISMATCH_CODE = "extension-dispatch-binding-mismatch"
ConnectionReadiness = Literal["ready", "disconnected", "authorization_required"]
_CATALOG_ONLY_PREFIX = "mcp-catalog-only:v1:"


class ExtensionCatalogBindingError(RuntimeError):
    """扩展目录 binding 的显式错误基类。"""


class ExtensionTargetConflictError(ExtensionCatalogBindingError):
    """同名 target 冲突；candidate 发布前必须显式拒绝。"""


class ExtensionTargetResolutionError(ExtensionCatalogBindingError):
    """sealed binding 中不存在该 target；旧调用按原 tool_call_id 显式失败。"""


class ExtensionCatalogUnavailableError(ExtensionCatalogBindingError):
    """历史 sealed binding 丢失或不完整；拒绝 dispatch，绝不回退当前目录。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"[{EXTENSION_CATALOG_UNAVAILABLE_CODE}] {message}")
        self.code = EXTENSION_CATALOG_UNAVAILABLE_CODE


class ExtensionDispatchBindingMismatchError(ExtensionCatalogBindingError):
    """重算 dispatch binding hash 与封存值不一致；拒绝继续调用。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"[{EXTENSION_DISPATCH_BINDING_MISMATCH_CODE}] {message}")
        self.code = EXTENSION_DISPATCH_BINDING_MISMATCH_CODE


def extension_schema_hash(input_schema: Mapping[str, object]) -> str:
    """完整公开输入契约的版本化 JCS 指纹，不接受 properties 摘要或 repr。"""
    return payload_digest(
        {"schema": "extension-input-schema:v2", "input_schema": input_schema},
        context="扩展工具完整输入契约", error_type=ExtensionCatalogBindingError,
    )


def sha256_token(data: bytes) -> str:
    """``sha256:<64位小写hex>`` 摘要 token 的唯一拼接实现。

    MCP 各摘要（目录 revision、指引 revision、activation provenance、target
    schema）共享同一条形；调用方不得再各写一份
    ``"sha256:" + hashlib.sha256(...).hexdigest()``。
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def payload_digest(
    payload: object,
    *,
    context: str,
    error_type: type[Exception],
) -> str:
    """canonical payload 的 ``sha256:<64hex>`` 摘要 token 唯一实现。

    MCP 各 payload 摘要（目录 revision、指引 revision、activation
    provenance）共享同一条 canonical JSON 编码与 digest 路径；调用方不得再
    各写一份 ``"sha256:" + hashlib.sha256(...)``。该 token 形状由
    ``mcp.catalog`` 事件与 guidance source port 合同固定，因此不带
    ``jcs:v1`` 版本段；需要可跨进程恢复的 domain hash token（``binding_hash``
    与 ``extension_dispatch_binding_hash``）走 ``sha256_jcs``。
    """
    try:
        payload_bytes = canonical_json_bytes(payload)
    except ItemSchemaError as error:
        raise error_type(f"{context} 无法 canonical 编码: {error}") from error
    return sha256_token(payload_bytes)


@dataclass(frozen=True, slots=True)
class ExtensionTargetBinding:
    """一个扩展 target 在 sealed binding 中的不可变条目。"""

    target_id: str
    origin: Literal["mcp", "custom"]
    schema_hash: str
    server_id: str | None = None
    connection_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("ExtensionTargetBinding.target_id 不能为空")
        if self.origin not in {"mcp", "custom"}:
            raise ValueError(f"ExtensionTargetBinding.origin 非法: {self.origin!r}")
        if not self.schema_hash.startswith("sha256:"):
            raise ValueError("ExtensionTargetBinding.schema_hash 必须是 sha256 指纹")
        if self.origin == "mcp" and not self.server_id:
            raise ValueError("MCP target 必须携带 server_id")
        if self.origin == "custom" and self.server_id is not None:
            raise ValueError("custom target 不得携带 server_id")
        if self.origin == "mcp":
            _validate_connection_ref(self.connection_ref)
        elif self.connection_ref is not None:
            raise ValueError("custom target 不得携带 connection_ref")


def _validate_connection_ref(value: str | None) -> str:
    """执行身份包含 Owner 实例和连接代际；不从历史语义 revision 推断。"""
    if not isinstance(value, str) or re.fullmatch(r"mcp-connection:v1:[0-9a-f]{32}:[1-9][0-9]*", value) is None:
        raise ExtensionCatalogUnavailableError("MCP target 缺少可核对的精确连接身份；历史定义可读，不能恢复执行")
    return value


def member_readiness(member: ResourceMemberBinding) -> ConnectionReadiness:
    """目录事实可保留已知契约，但目录专用成员不能成为执行连接。"""
    if member.member_kind == "custom":
        return "ready"
    if member.revision == _CATALOG_ONLY_PREFIX + "disconnected":
        return "disconnected"
    if member.revision == _CATALOG_ONLY_PREFIX + "authorization_required":
        return "authorization_required"
    _validate_connection_ref(member.revision)
    return "ready"


def _binding_id(
    generation: int, catalog_revision: str, members: tuple[ResourceMemberBinding, ...],
) -> str:
    # 相同 schema 的重连保持语义 revision，但不能复用旧 binding identity。
    member_hash = sha256_jcs([member.to_dict() for member in sorted(members, key=lambda member: member.member_id)])
    return f"{_BINDING_ID_PREFIX}:{generation}:{catalog_revision}:{member_hash}"


@dataclass(frozen=True, slots=True)
class ExtensionCatalogBindingRef:
    """一次 model-call preparation 封存的扩展目录 binding。

    binding_id 由 generation、catalog revision 与完整 members 确定性导出；binding_hash
    以 ``sha256:jcs:v1`` 覆盖本 ref 的全部字段并可重算核对。目录内部 target
    增删改或权限变化不改变已封存 ref；只有 Provider 信封形状变化才更换
    provider_binding_identity 并进入 hard rebase 合同。
    """

    binding_id: str
    catalog_revision: str
    generation: int
    provider_binding_identity: str
    members: tuple[ResourceMemberBinding, ...]
    binding_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding_id, str) or not self.binding_id:
            raise ValueError("ExtensionCatalogBindingRef.binding_id 不能为空")
        if not self.catalog_revision.startswith("sha256:"):
            raise ValueError(
                "ExtensionCatalogBindingRef.catalog_revision 必须是 sha256 revision"
            )
        if self.generation < 1:
            raise ValueError("ExtensionCatalogBindingRef.generation 必须 >= 1")
        if not self.provider_binding_identity:
            raise ValueError(
                "ExtensionCatalogBindingRef.provider_binding_identity 不能为空"
            )
        if not isinstance(self.members, tuple) or not all(
            isinstance(member, ResourceMemberBinding) for member in self.members
        ):
            raise TypeError("ExtensionCatalogBindingRef.members 必须是领域成员元组")
        if self.binding_id != _binding_id(self.generation, self.catalog_revision, self.members):
            raise ExtensionCatalogUnavailableError(
                "binding 契约版本不支持恢复执行；历史记录可读，必须从新模型调用获取完整定义"
            )
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ExtensionTargetConflictError("extension catalog member_id 必须唯一")
        for member in self.members:
            member_readiness(member)
        object.__setattr__(
            self, "members", tuple(sorted(self.members, key=lambda member: member.member_id)),
        )
        try:
            binding_hash = sha256_jcs(self.binding_preimage())
        except ItemSchemaError as error:
            raise ExtensionCatalogBindingError(
                f"extension catalog binding 载荷无法 canonical 编码: {error}"
            ) from error
        object.__setattr__(self, "binding_hash", binding_hash)

    @property
    def targets(self) -> Mapping[str, ExtensionTargetBinding]:
        """从唯一 members 构造执行只读视图；不保存第二套 target 权威集合。"""

        return MappingProxyType({
            member.member_id: ExtensionTargetBinding(
                target_id=member.member_id, origin=member.member_kind,
                schema_hash=member.schema_hash, server_id=member.server_id,
                connection_ref=member.revision if member.member_kind == "mcp" else None,
            )
            for member in self.members
            if member_readiness(member) == "ready"
        })

    def binding_preimage(self) -> dict[str, object]:
        """本 ref 的版本化 canonical payload；``binding_hash`` 的唯一来源。"""
        return {
            "schema": EXTENSION_CATALOG_BINDING_SCHEMA,
            "binding_id": self.binding_id,
            "catalog_revision": self.catalog_revision,
            "generation": self.generation,
            "provider_binding_identity": self.provider_binding_identity,
            "members": [member.to_dict() for member in self.members],
        }

    def resolve(self, tool_name: str) -> ExtensionTargetBinding:
        """按 sealed ref 解析精确 target；缺失显式失败，不回退 live 目录。"""
        target = self.targets.get(tool_name)
        if target is None:
            if any(member.member_id == tool_name for member in self.members):
                raise ExtensionTargetResolutionError(f"sealed target 的连接未就绪，须重新连接并取得新目录: {tool_name}")
            raise ExtensionTargetResolutionError(
                "sealed extension catalog binding 中不存在 target: "
                f"tool_name={tool_name!r} binding_id={self.binding_id}"
            )
        return target


@dataclass(frozen=True, slots=True)
class ExtensionTargetBindingInput:
    """binding 构建输入；完整公开 input_schema 在构建时冻结为身份。"""

    target_id: str
    origin: Literal["mcp", "custom"]
    input_schema: Mapping[str, object]
    server_id: str | None = None
    connection_ref: str | None = None
    readiness: ConnectionReadiness = "ready"


def build_extension_catalog_binding(
    *,
    catalog_revision: str,
    generation: int,
    targets: Iterable[ExtensionTargetBindingInput],
    provider_binding_identity: str = EXTENSION_TOOL_ENVELOPE_IDENTITY,
) -> ExtensionCatalogBindingRef:
    """从已验证目录条目构建 sealed binding；同名冲突发布前显式拒绝。"""
    entries: dict[str, ResourceMemberBinding] = {}
    for item in targets:
        if item.readiness not in {"ready", "disconnected", "authorization_required"}:
            raise ValueError("扩展目录 readiness 非法")
        if item.readiness != "ready":
            if item.origin != "mcp" or item.connection_ref is not None:
                raise ValueError("未就绪成员必须属于 MCP 且不得携带执行连接")
            revision = _CATALOG_ONLY_PREFIX + item.readiness
        else:
            revision = _validate_connection_ref(item.connection_ref) if item.origin == "mcp" else catalog_revision
        if item.origin != "mcp" and item.connection_ref is not None:
            raise ValueError("custom target 不得携带 connection_ref")
        if item.target_id in entries:
            raise ExtensionTargetConflictError(
                "扩展 target 公共名称冲突，发布前必须显式拒绝: "
                f"target_id={item.target_id!r}"
            )
        entries[item.target_id] = ResourceMemberBinding(
            member_id=item.target_id,
            member_kind=item.origin,
            schema_hash=extension_schema_hash(item.input_schema),
            server_id=item.server_id,
            # typed member revision 表达实际可执行成员版本；目录语义另有独立字段。
            revision=revision,
        )
    return ExtensionCatalogBindingRef(
        binding_id=_binding_id(generation, catalog_revision, tuple(entries.values())),
        catalog_revision=catalog_revision,
        generation=generation,
        provider_binding_identity=provider_binding_identity,
        members=tuple(entries.values()),
    )


def extension_catalog_binding_from_members(
    *, catalog_revision: str, generation: int,
    members: tuple[ResourceMemberBinding, ...],
    provider_binding_identity: str = EXTENSION_TOOL_ENVELOPE_IDENTITY,
) -> ExtensionCatalogBindingRef:
    """typed members 是唯一集合；restore 不读取 live tools 或重建 schema。"""
    return ExtensionCatalogBindingRef(
        binding_id=_binding_id(generation, catalog_revision, members),
        catalog_revision=catalog_revision, generation=generation,
        provider_binding_identity=provider_binding_identity, members=members,
    )


@dataclass(frozen=True, slots=True)
class ExtensionDispatchBindingRef:
    """一次 ModelCallResourceSnapshot 原子绑定的扩展 dispatch binding。

    它把 sealed :class:`ExtensionCatalogBindingRef`（catalog semantic
    revision/hash、server generation 验证 ref、稳定 target/schema 集合）与同一
    次 seal 的 MCP 指引 revision、activation policy revision 原子绑定，并导出
    独立的 ``extension_dispatch_binding_hash``。该 hash 只服务 dispatch/restore
    校验，不进入 ``context-plan-hash:v2``、Provider ``ToolSetRef`` 或
    ``request_hash``；因此内层目录/指引变化不会制造 ``toolset_changed`` epoch。

    受保护 catalog snapshot/detail ref 与运行 identity（owner/turn/model-call）
    只作 typed relation，不进入内容 hash。
    """

    binding_ref: ExtensionCatalogBindingRef
    guidance_revision: str
    activation_policy_revision: str
    owner_session_id: str
    owner_thread_id: str
    execution_id: str
    turn_id: str | None
    model_call_id: str | None = None
    # 受保护 catalog snapshot/detail 的 typed ref；冷历史只保留验证 ref。
    catalog_snapshot_ref: str | None = None
    dispatch_binding_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding_ref, ExtensionCatalogBindingRef):
            raise TypeError(
                "ExtensionDispatchBindingRef.binding_ref 必须是 "
                "ExtensionCatalogBindingRef"
            )
        for name in (
            "guidance_revision",
            "activation_policy_revision",
            "owner_session_id",
            "owner_thread_id",
            "execution_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"ExtensionDispatchBindingRef.{name} 必须是非空字符串"
                )
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id
        ):
            raise ValueError("ExtensionDispatchBindingRef.turn_id 必须是非空字符串或 None")
        if self.model_call_id is not None and (
            not isinstance(self.model_call_id, str) or not self.model_call_id
        ):
            raise ValueError(
                "ExtensionDispatchBindingRef.model_call_id 必须是非空字符串或 None"
            )
        if self.catalog_snapshot_ref is not None and (
            not isinstance(self.catalog_snapshot_ref, str)
            or not self.catalog_snapshot_ref
        ):
            raise ValueError(
                "ExtensionDispatchBindingRef.catalog_snapshot_ref 必须是非空字符串或 None"
            )
        try:
            dispatch_hash = sha256_jcs(self.dispatch_preimage())
        except ItemSchemaError as error:
            raise ExtensionCatalogBindingError(
                f"extension dispatch binding 载荷无法 canonical 编码: {error}"
            ) from error
        object.__setattr__(self, "dispatch_binding_hash", dispatch_hash)

    def dispatch_preimage(self) -> dict[str, object]:
        """版本化 canonical payload；``extension_dispatch_binding_hash`` 唯一来源。"""
        return {
            "schema": EXTENSION_DISPATCH_BINDING_SCHEMA,
            "catalog_revision": self.binding_ref.catalog_revision,
            "catalog_binding_hash": self.binding_ref.binding_hash,
            "generation": self.binding_ref.generation,
            "provider_binding_identity": self.binding_ref.provider_binding_identity,
            "guidance_revision": self.guidance_revision,
            "activation_policy_revision": self.activation_policy_revision,
            "members": [member.to_dict() for member in self.binding_ref.members],
        }

    def verify(self) -> ExtensionDispatchBindingRef:
        """重算 dispatch hash 并核对；不一致显式拒绝继续调用。"""
        recomputed = sha256_jcs(self.dispatch_preimage())
        if recomputed != self.dispatch_binding_hash:
            raise ExtensionDispatchBindingMismatchError(
                "extension dispatch binding hash 与重算结果不一致: "
                f"binding_id={self.binding_ref.binding_id}"
            )
        return self

    @classmethod
    def from_sealed_snapshot(cls, value: object) -> ExtensionDispatchBindingRef:
        """从封存 snapshot 恢复 dispatch binding；缺失显式 ``extension-catalog-unavailable``。

        历史 binding 丢失时绝不允许从当前 MCP 目录、同名 target 或空目录
        回退；调用方必须拒绝 dispatch。
        """
        if not isinstance(value, Mapping):
            raise ExtensionCatalogUnavailableError(
                "历史 extension dispatch binding 丢失或不完整，拒绝 dispatch"
            )
        required = {
            "binding_id",
            "catalog_revision",
            "generation",
            "provider_binding_identity",
            "guidance_revision",
            "activation_policy_revision",
            "owner_session_id",
            "owner_thread_id",
            "execution_id",
            "turn_id",
            "members",
            "extension_dispatch_binding_hash",
        }
        missing = required - set(value)
        if missing:
            raise ExtensionCatalogUnavailableError(
                "历史 extension dispatch binding 缺少字段: " f"{sorted(missing)}"
            )
        raw_members = value["members"]
        if not isinstance(raw_members, list):
            raise ExtensionCatalogUnavailableError("历史 extension binding members 必须是数组")
        members = tuple(ResourceMemberBinding.from_dict(member) for member in raw_members)
        model_call_id = value.get("model_call_id")
        catalog_snapshot_ref = value.get("catalog_snapshot_ref")
        try:
            restored = cls(
                binding_ref=ExtensionCatalogBindingRef(
                    binding_id=str(value["binding_id"]),
                    catalog_revision=str(value["catalog_revision"]),
                    generation=int(value["generation"]),
                    provider_binding_identity=str(
                        value["provider_binding_identity"]
                    ),
                    members=members,
                ),
                guidance_revision=str(value["guidance_revision"]),
                activation_policy_revision=str(
                    value["activation_policy_revision"]
                ),
                owner_session_id=str(value["owner_session_id"]),
                owner_thread_id=str(value["owner_thread_id"]),
                execution_id=value["execution_id"],
                turn_id=value["turn_id"],
                model_call_id=(
                    None if model_call_id is None else str(model_call_id)
                ),
                catalog_snapshot_ref=(
                    None
                    if catalog_snapshot_ref is None
                    else str(catalog_snapshot_ref)
                ),
            ).verify()
        except (TypeError, ValueError) as error:
            raise ExtensionCatalogUnavailableError(
                "历史 extension dispatch binding 字段非法，拒绝 dispatch: "
                f"{error}"
            ) from error
        declared_hash = value.get("extension_dispatch_binding_hash")
        if declared_hash is not None and declared_hash != restored.dispatch_binding_hash:
            raise ExtensionDispatchBindingMismatchError(
                "历史 extension dispatch binding hash 与重算结果不一致，拒绝 dispatch"
            )
        return restored

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_ref.binding_id,
            "catalog_revision": self.binding_ref.catalog_revision,
            "generation": self.binding_ref.generation,
            "provider_binding_identity": self.binding_ref.provider_binding_identity,
            "guidance_revision": self.guidance_revision,
            "activation_policy_revision": self.activation_policy_revision,
            "owner_session_id": self.owner_session_id,
            "owner_thread_id": self.owner_thread_id,
            "execution_id": self.execution_id,
            "turn_id": self.turn_id,
            "model_call_id": self.model_call_id,
            "catalog_snapshot_ref": self.catalog_snapshot_ref,
            "extension_dispatch_binding_hash": self.dispatch_binding_hash,
            "members": [member.to_dict() for member in self.binding_ref.members],
        }
