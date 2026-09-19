"""ExtensionCatalogBindingRef：model-call preparation 封存的扩展目录 binding。

OpenSpec add-context-injection-lifecycle E4：dispatcher/extension invoker 只按
sealed binding ref 解析旧 tool call；目录后续增删改/权限变化不影响已封存
ref，执行点按最新权限校验并返回真实 paired result。binding ref 是不可变、
可验证（binding_id 与 binding_hash 均可重算核对）的 typed value object。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.domain.itemized.hashing import ItemSchemaError, canonical_json_bytes

# 固定 Provider 信封的 binding identity。信封名称/参数形状变化必须产生新的
# provider_binding_identity，才允许进入 ToolSet hard rebase 合同（E3/E4 边界）。
EXTENSION_TOOL_ENVELOPE_IDENTITY = (
    "invoke_extension_tool@v1:tool_name:str+arguments:object"
)

_BINDING_ID_PREFIX = "ext-catalog:v1"
_BINDING_HASH_DOMAIN = "extension-catalog-binding:v1"


class ExtensionCatalogBindingError(RuntimeError):
    """扩展目录 binding 的显式错误基类。"""


class ExtensionTargetConflictError(ExtensionCatalogBindingError):
    """同名 target 冲突；candidate 发布前必须显式拒绝。"""


class ExtensionTargetResolutionError(ExtensionCatalogBindingError):
    """sealed binding 中不存在该 target；旧调用按原 tool_call_id 显式失败。"""


def extension_args_fingerprint(args: Mapping[str, object]) -> str:
    """工具参数 schema 的 canonical JSON 指纹（参与目录语义 revision 载荷）。"""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(sorted(args))


def extension_schema_hash(args: Mapping[str, object]) -> str:
    """target 参数 schema 的 sha256 身份；进入 binding 条目并可重算核对。"""
    fingerprint = extension_args_fingerprint(args)
    return "sha256:" + hashlib.sha256(fingerprint.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExtensionTargetBinding:
    """一个扩展 target 在 sealed binding 中的不可变条目。"""

    target_id: str
    origin: str
    schema_hash: str
    server_id: str | None = None

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


@dataclass(frozen=True, slots=True)
class ExtensionCatalogBindingRef:
    """一次 model-call preparation 封存的扩展目录 binding。

    binding_id 由 generation 与 catalog revision 确定性导出；binding_hash
    覆盖全部字段并可重算核对。目录内部 target 增删改或权限变化不改变已
    封存 ref；只有 Provider 信封形状变化才更换 provider_binding_identity
    并进入 hard rebase 合同。
    """

    binding_id: str
    catalog_revision: str
    generation: int
    provider_binding_identity: str
    targets: Mapping[str, ExtensionTargetBinding]
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
        for target_id, target in self.targets.items():
            if target.target_id != target_id:
                raise ValueError(
                    "ExtensionCatalogBindingRef.targets 键必须等于 target_id: "
                    f"key={target_id!r} target_id={target.target_id!r}"
                )
        payload = [
            _BINDING_HASH_DOMAIN,
            self.binding_id,
            self.catalog_revision,
            self.generation,
            self.provider_binding_identity,
            [
                [t.target_id, t.origin, t.server_id, t.schema_hash]
                for t in sorted(self.targets.values(), key=lambda item: item.target_id)
            ],
        ]
        try:
            payload_bytes = canonical_json_bytes(payload)
        except ItemSchemaError as error:
            raise ExtensionCatalogBindingError(
                f"extension catalog binding 载荷无法 canonical 编码: {error}"
            ) from error
        object.__setattr__(
            self,
            "binding_hash",
            "sha256:" + hashlib.sha256(payload_bytes).hexdigest(),
        )

    def resolve(self, tool_name: str) -> ExtensionTargetBinding:
        """按 sealed ref 解析精确 target；缺失显式失败，不回退 live 目录。"""
        target = self.targets.get(tool_name)
        if target is None:
            raise ExtensionTargetResolutionError(
                "sealed extension catalog binding 中不存在 target: "
                f"tool_name={tool_name!r} binding_id={self.binding_id}"
            )
        return target


@dataclass(frozen=True, slots=True)
class ExtensionTargetBindingInput:
    """binding 构建输入；args 为工具参数 schema，schema_hash 在构建时冻结。"""

    target_id: str
    origin: str
    args: Mapping[str, object]
    server_id: str | None = None


def build_extension_catalog_binding(
    *,
    catalog_revision: str,
    generation: int,
    targets: Iterable[ExtensionTargetBindingInput],
    provider_binding_identity: str = EXTENSION_TOOL_ENVELOPE_IDENTITY,
) -> ExtensionCatalogBindingRef:
    """从已验证目录条目构建 sealed binding；同名冲突发布前显式拒绝。"""
    entries: dict[str, ExtensionTargetBinding] = {}
    for item in targets:
        if item.target_id in entries:
            raise ExtensionTargetConflictError(
                "扩展 target 公共名称冲突，发布前必须显式拒绝: "
                f"target_id={item.target_id!r}"
            )
        entries[item.target_id] = ExtensionTargetBinding(
            target_id=item.target_id,
            origin=item.origin,
            schema_hash=extension_schema_hash(item.args),
            server_id=item.server_id,
        )
    return ExtensionCatalogBindingRef(
        binding_id=f"{_BINDING_ID_PREFIX}:{generation}:{catalog_revision}",
        catalog_revision=catalog_revision,
        generation=generation,
        provider_binding_identity=provider_binding_identity,
        targets=MappingProxyType(entries),
    )
