"""语义派生链的纯值对象与 loader port。

本模块只承载不可变数据合同,不执行 I/O,也不导入 registry/graph,
供 registry 与 graph 双向安全引用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_SNAPSHOT_ERROR_CODES = frozenset(
    {
        "dependency-missing",
        "dependency-unavailable",
        "generation-mismatch",
        "source-unavailable",
        "loader-error",
        "dependency-cycle",
    }
)


@dataclass(frozen=True, slots=True)
class SemanticResourceDescriptor:
    """语义资源的不可变描述:identity/display URI/来源绑定。

    ``resource_id`` 是语义资源稳定 identity(如 skill:demo:metadata),
    ``facet`` 标记同一来源可派生的独立语义面(metadata/activation/config
    等,由代码内注册方决定);``source_ids`` 是依赖的已登记来源,
    ``dependency_resource_ids`` 是依赖的其它语义资源(AGENTS 链等)。
    """

    resource_id: str
    resource_kind: str
    facet: str
    display_uri: str
    source_ids: tuple[str, ...] = ()
    dependency_resource_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("resource_id", "resource_kind", "facet", "display_uri"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"SemanticResourceDescriptor.{field_name} 必须是非空字符串"
                )
        if not self.display_uri.startswith("boxteam://"):
            raise ValueError(
                "SemanticResourceDescriptor.display_uri 必须是 boxteam:// 虚拟 URI"
            )
        for tuple_field in ("source_ids", "dependency_resource_ids"):
            value = getattr(self, tuple_field)
            if (
                not isinstance(value, tuple)
                or any(not isinstance(item, str) or not item for item in value)
            ):
                raise ValueError(
                    f"SemanticResourceDescriptor.{tuple_field} 必须是非空字符串元组"
                )
        if len(set(self.source_ids)) != len(self.source_ids) or len(
            set(self.dependency_resource_ids)
        ) != len(self.dependency_resource_ids):
            raise ValueError("SemanticResourceDescriptor 依赖 identity 不得重复")


@dataclass(frozen=True, slots=True)
class SemanticInput:
    """派生 loader 的单条输入:依赖 identity、revision 与代际。"""

    identity: str
    revision: str
    generation: int
    content: object = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class SemanticPayload:
    """loader 的解析结果:语义 facet payload。

    payload 必须是 JSON value(语义 revision 取其 JCS hash);来源无效时
    loader 显式返回 available=False 与 error,而不是抛异常。
    """

    payload: object = None
    available: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """语义 registry 发布的不可变快照。

    revision 是语义 revision(payload 的 sha256:jcs:v1),与来源 raw
    revision 分离;source_lineage 记录本次发布消费的全部依赖 revision。
    unavailable 时 payload 为 None,revision/retained_revision 保留旧
    valid 事实以供审计。
    """

    resource_id: str
    resource_kind: str
    facet: str
    display_uri: str
    revision: str
    payload: object
    source_lineage: tuple[tuple[str, str], ...]
    generation: int
    available: bool = True
    error: str | None = None
    error_code: str | None = None
    retained_revision: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("resource_id", "resource_kind", "facet", "display_uri"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ResourceSnapshot.{field_name} 必须是非空字符串")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("ResourceSnapshot.generation 必须是非负整数")
        lineage = self.source_lineage
        if (
            not isinstance(lineage, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                for item in lineage
            )
        ):
            raise ValueError(
                "ResourceSnapshot.source_lineage 必须是 (identity, revision) 元组"
            )
        if self.available:
            if (
                not self.revision
                or self.error is not None
                or self.retained_revision is not None
            ):
                raise ValueError(
                    "available snapshot 必须有 revision 且不得携带错误/保留字段"
                )
            if self.payload is None:
                raise ValueError("available snapshot 的 payload 不得为 None")
            return
        if self.payload is not None or self.error is None:
            raise ValueError("unavailable snapshot 必须置空 payload 并携带显式 error")
        if self.error_code not in _SNAPSHOT_ERROR_CODES:
            raise ValueError(f"未知 ResourceSnapshot.error_code: {self.error_code}")
        if self.retained_revision is not None and not self.retained_revision:
            raise ValueError("retained_revision 必须是非空字符串或 None")


@runtime_checkable
class SemanticLoader(Protocol):
    """版本化语义 loader port;业务 loader 由对应 domain owner 代码内注册。

    loader 只消费输入的 revision/content/payload,不得执行 I/O 或读取
    当前文件;同版本 loader 对相同输入必须产出相同 payload。
    """

    def load(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload: ...
