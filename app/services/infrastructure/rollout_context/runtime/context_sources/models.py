"""Context source runtime 的纯值合同与结果状态。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

SkillLoadMode = Literal["snapshot", "tracked", "untrack"]


@dataclass(frozen=True, slots=True)
class ContextSourceDescriptor:
    """来源的内部描述；locator 只存在于服务端。"""

    source_id: str
    source_kind: str
    name: str
    description: str
    internal_locator: str
    resource_uri: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_id",
            "source_kind",
            "name",
            "internal_locator",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ContextSourceDescriptor.{field_name} 必须是非空字符串")
        if not isinstance(self.description, str):
            raise TypeError("ContextSourceDescriptor.description 必须是字符串")
        if self.resource_uri is not None and (
            not isinstance(self.resource_uri, str) or not self.resource_uri
        ):
            raise ValueError(
                "ContextSourceDescriptor.resource_uri 必须是非空字符串或 None"
            )


@dataclass(frozen=True, slots=True)
class PendingSourceObservation:
    """等待 CSM 消费的一条轻量 observation 标记。

    该值只描述「某来源需要读取哪个 revision」；正文始终由来源 owner 的权威
    内存快照提供，CSM 不执行 I/O。``revision is None`` 表示只知道「该来源可能
    有新事实」，由消费方按权威快照对账（用于通知丢失后的全量 reconcile）。
    """

    source_id: str
    revision: str | None


@dataclass(frozen=True, slots=True)
class ContextSourceDelta:
    """准备交给唯一 ContextStore owner 的上下文尾部 item。

    previous_revision 是 from 基准，恒等于提交时刻的 latest visible
    committed revision；content_hash 是注入载荷（diff 或完整正文）的
    确定性 hash；observation_provenance 按观察顺序记录被合并进本 delta
    的 observed revisions，不包含 from 基准本身。多个 pending
    observation 在提交前合并为唯一 delta，不产生中间 context item。
    """

    source_id: str
    source_name: str
    source_kind: str
    revision: str
    previous_revision: str | None
    content: str
    content_hash: str
    observation_provenance: tuple[str, ...] = ()
    wire_role: Literal["user"] = "user"
    kind: Literal["activation", "delta", "rebuild"] = "activation"


SkillLoadStatus = Literal["loaded", "already_active", "not_tracked"]
SkillLoadAppendStatus = Literal["appended", "already_active", "none"]


class ContextSourceTrackingStateConflict(RuntimeError):
    """同一 (owner thread, normalized name) 存在多个 active registration。

    OpenSpec 3.14 的闭合错误合同：检测到重复 active tracked registration
    时 fail closed，code 固定为 tracking-state-conflict，不静默挑选
    其中一个 registration，也不伪造成功状态。
    """

    code = "tracking-state-conflict"


class SkillCatalogSnapshotConflict(RuntimeError):
    """skill_load 的冻结 SkillCatalog binding 与运行态不一致。

    OpenSpec 4.3 闭合错误合同：冻结 Turn/ModelCall SkillCatalogSnapshot
    缺失、binding 与已登记来源不一致或 activation revision 与 live 状态
    冲突时 fail closed，code 固定为 skill-catalog-snapshot-conflict，
    不回退读盘，也不静默改用当前 catalog。
    """

    code = "skill-catalog-snapshot-conflict"


def _normalize_skill_name(name: str) -> str:
    """Skill 逻辑名的唯一归一化：剥离首尾空白；大小写保持敏感。

    与虚拟资源 grammar 的固定 segment 一致，catalog 名称大小写敏感，
    normalized name 只做空白归一，避免同一 registration 出现两个索引键。
    """
    return name.strip()


@dataclass(frozen=True, slots=True)
class SkillLoadReceipt:
    """skill_load 的脱敏返回值，不包含 locator、credential 或正文。

    ``display_uri`` 是模型可见的 boxteam:// 虚拟资源 URI，属于
    ResourceProvenance 而非 Skill metadata；未绑定 descriptor 的恢复
    registration 返回 None，不得伪造 URI。``revision`` 是 source/semantic
    revision（当前为内容寻址 sha256，catalog revision producer 落地后两者
    分离）；``content_hash`` 是精确正文的 hash 标识，永远不含正文本身。
    """

    name: str
    mode: SkillLoadMode
    revision: str | None
    queued: bool
    tracked: bool
    display_uri: str | None = None
    content_hash: str | None = None
    status: SkillLoadStatus = "loaded"
    append_status: SkillLoadAppendStatus = "none"


@dataclass(frozen=True, slots=True)
class SkillCatalogBinding:
    """冻结 SkillCatalog activation binding。

    body 是 D1 发布的 activation facet 精确正文（frontmatter 之后），
    冻结进 binding 后 skill_load 在工具路径零 I/O 解析；模型可见结果
    永远不含 body。
    """

    name: str
    resource_id: str
    entry_identity: str
    display_uri: str
    activation_revision: str
    body: str
    content_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "name",
            "resource_id",
            "entry_identity",
            "display_uri",
            "activation_revision",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"SkillCatalogBinding.{field_name} 必须是非空字符串"
                )
        if not isinstance(self.body, str):
            raise TypeError("SkillCatalogBinding.body 必须是字符串")
        object.__setattr__(
            self,
            "content_hash",
            "sha256:" + hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class SkillCatalogActivationSnapshot:
    """冻结在 Turn/ModelCall 边界的 SkillCatalog activation snapshot。

    snapshot/tracked 只从本 snapshot 的 exact binding 解析；同名 catalog
    新 revision 不会改变已冻结调用，untrack 也不读本 snapshot。
    """

    catalog_revision: str
    entries: Mapping[str, SkillCatalogBinding]

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_revision, str) or not self.catalog_revision:
            raise ValueError(
                "SkillCatalogActivationSnapshot.catalog_revision 必须是非空字符串"
            )
        if not isinstance(self.entries, Mapping):
            raise TypeError("SkillCatalogActivationSnapshot.entries 必须是 Mapping")
        for name, binding in self.entries.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "SkillCatalogActivationSnapshot entry 名必须是非空字符串"
                )
            if binding.name != name:
                raise ValueError(
                    "SkillCatalogActivationSnapshot entry 名与 binding.name 不一致: "
                    f"{name!r} != {binding.name!r}"
                )
@dataclass(frozen=True, slots=True)
class PendingContextSourceBatch:
    """一次待提交的来源变更准备结果。

    prepare_pending 只读：不推进 applied revision，不改变任何持久状态。
    原子提交走 commit_model_call_pending：先经唯一 durable 端口逐条持久化
    提交后控制状态，全部成功后才推进内存 applied、清空 pending 并发布事件；
    重复提交同一批次复用同一结果。
    """

    deltas: tuple[ContextSourceDelta, ...]


@dataclass(frozen=True, slots=True)
class CommittedContextSourceBatch:
    """一次 model_call pending 提交的结果；重复提交复用同一结果对象。"""

    deltas: tuple[ContextSourceDelta, ...]

__all__ = [
    "CommittedContextSourceBatch", "ContextSourceDelta", "ContextSourceDescriptor",
    "ContextSourceTrackingStateConflict", "PendingContextSourceBatch",
    "PendingSourceObservation", "SkillCatalogActivationSnapshot",
    "SkillCatalogBinding", "SkillCatalogSnapshotConflict",
    "SkillLoadAppendStatus", "SkillLoadMode", "SkillLoadReceipt", "SkillLoadStatus",
]
