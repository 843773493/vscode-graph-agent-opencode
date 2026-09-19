"""Root compilation 与 post-user user-role projection 的领域合同。

OpenSpec add-context-injection-lifecycle E1(任务 5.1/5.2)的第一个纵向切片:
root 资格由 source owner 用 typed root_placement=root_eligible|tail_only 显式
声明,CSM/compiler 不得从路径、文本或 extensions 推断。每个合法 prefix epoch
的最顶层 root context 至多投影为一个 wire_role=system source item;只有首次
组装(initial)与实际 compaction、rewind、Provider ToolSet hard rebase 开启的
新 epoch 才能编译 root,tail_only 来源(包括默认外部 MCP 工具指引)永远只能
作为独立 user-role item 追加。

本模块是纯值合同:只消费本包 prefix_epoch 已落地的 PrefixEpochState/PrefixEpoch,
不读取 rollout storage、CSM 运行态、Provider 请求或自由 metadata/extensions。
生产 epoch owner(任务 5.1 的 plan compiler 接线)在新 epoch 物化时调用本合同;
任何违反直接抛出闭合错误码,不静默降级、不伪造空 root。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    contribution_content_hash,
    sha256_jcs,
    validate_hash_token,
)
from app.domain.itemized.prefix_epoch import (
    REBUILD_EPOCH_REASONS,
    EpochReason,
    PrefixEpoch,
    PrefixEpochState,
)

RootPlacement = Literal["root_eligible", "tail_only"]
"""source owner 声明的根资格;默认外部内容与未受信指引恒为 tail_only。"""

WireRole = Literal["system", "user"]
"""CSM source item 的投影 wire role;canonical user/assistant/tool 事实不在此列。"""

IncludedReason = Literal["initial", "toolset_policy", "epoch_rebuild"]
"""root 编译时记录的 included reason 闭合集合。

initial 是首次组装的基础说明/AGENTS/Skill metadata 等初始贡献;
toolset_policy 是绑定精确 ToolSet policy 的条件化 producer;
epoch_rebuild 是 compaction/rewind/hard rebase 新 epoch 吸收的完整有效状态。
"""

_INCLUDED_REASONS: frozenset[str] = frozenset(
    {"initial", "toolset_policy", "epoch_rebuild"}
)
_ROOT_PLACEMENTS: frozenset[str] = frozenset({"root_eligible", "tail_only"})


class RootCompileBoundaryError(ItemSchemaError):
    """在非法边界编译 root:epoch 已 applied 或来源候选非法。"""

    error_code = "root-compile-boundary-violation"


class RootCompileConflictError(ItemSchemaError):
    """同一 root 内出现重复 source identity/ordinal,或同 epoch 二次编译。"""

    error_code = "root-compile-conflict"


class RootPlacementViolationError(ItemSchemaError):
    """tail_only 来源试图进入 root,或 wire role 决策与资格声明矛盾。"""

    error_code = "root-placement-violation"


def _non_empty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ItemSchemaError(f"{field_name} 必须是非负整数")
    return value


@dataclass(frozen=True, slots=True)
class RootCandidateSource:
    """一次 root 编译的候选来源;资格由 owner 显式声明,编译器不推断。

    tail_only 候选传进编译器即失败:它属于 user-role 追加路径,一旦出现在
    root 编译输入里,说明调用方已把不可信内容混入 root 链路,必须显式崩溃
    而不是静默丢弃或合并。
    """

    source_identity: str
    source_kind: str
    name: str
    revision: str
    content: str
    source_ordinal: int
    root_placement: RootPlacement
    included_reason: IncludedReason

    def __post_init__(self) -> None:
        for field_name in ("source_identity", "source_kind", "name"):
            _non_empty_str(
                getattr(self, field_name), f"RootCandidateSource.{field_name}"
            )
        validate_hash_token(self.revision, "RootCandidateSource.revision")
        _non_empty_str(self.content, "RootCandidateSource.content")
        _non_negative_int(self.source_ordinal, "RootCandidateSource.source_ordinal")
        if self.root_placement not in _ROOT_PLACEMENTS:
            raise RootPlacementViolationError(
                f"未知 root_placement: {self.root_placement!r};"
                "资格只能由 source owner 显式声明为 root_eligible|tail_only"
            )
        if self.included_reason not in _INCLUDED_REASONS:
            raise RootCompileBoundaryError(
                f"未知 RootCandidateSource.included_reason: {self.included_reason!r}"
            )

    @property
    def content_hash(self) -> str:
        """确定性内容 hash,复用 contribution 内容 hash 的固定 JCS preimage。"""
        return contribution_content_hash("root_source", self.content)


@dataclass(frozen=True, slots=True)
class CompiledRootSourceProvenance:
    """root item 内单个来源的封存 provenance,进 root frame manifest。"""

    source_identity: str
    source_kind: str
    name: str
    revision: str
    content_hash: str
    source_ordinal: int
    included_reason: IncludedReason

    def __post_init__(self) -> None:
        for field_name in ("source_identity", "source_kind", "name"):
            _non_empty_str(
                getattr(self, field_name),
                f"CompiledRootSourceProvenance.{field_name}",
            )
        validate_hash_token(self.revision, "CompiledRootSourceProvenance.revision")
        validate_hash_token(
            self.content_hash, "CompiledRootSourceProvenance.content_hash"
        )
        _non_negative_int(
            self.source_ordinal, "CompiledRootSourceProvenance.source_ordinal"
        )
        if self.included_reason not in _INCLUDED_REASONS:
            raise RootCompileBoundaryError(
                "未知 CompiledRootSourceProvenance.included_reason: "
                f"{self.included_reason!r}"
            )


@dataclass(frozen=True, slots=True)
class CompiledRootItem:
    """一个合法 prefix epoch 的唯一最顶层 root system item manifest。

    sources 按 (source_ordinal, source_identity) 确定序排列且 ordinal 严格
    递增;frame_hash 是固定 JCS 算法的 root frame hash,同 epoch 重复编译或
    来源变化都会得到不同 hash,供 seal/preflight 复核。
    """

    epoch: PrefixEpoch
    provider_profile: str
    tool_compatibility_key: str
    content: str
    sources: tuple[CompiledRootSourceProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.epoch, PrefixEpoch):
            raise RootCompileBoundaryError("CompiledRootItem.epoch 必须是 PrefixEpoch")
        _non_empty_str(self.provider_profile, "CompiledRootItem.provider_profile")
        validate_hash_token(
            self.tool_compatibility_key, "CompiledRootItem.tool_compatibility_key"
        )
        _non_empty_str(self.content, "CompiledRootItem.content")
        if not self.sources:
            raise RootCompileBoundaryError("CompiledRootItem.sources 不能为空")
        for provenance in self.sources:
            if not isinstance(provenance, CompiledRootSourceProvenance):
                raise TypeError(
                    "CompiledRootItem.sources 只能包含 CompiledRootSourceProvenance"
                )
        ordinals = [provenance.source_ordinal for provenance in self.sources]
        if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
            raise RootCompileConflictError(
                "CompiledRootItem.sources 必须按 source_ordinal 严格递增排列"
            )
        identities = [provenance.source_identity for provenance in self.sources]
        if len(set(identities)) != len(identities):
            raise RootCompileConflictError(
                "同一 root 内 source identity 必须唯一,避免重复选择"
            )

    @property
    def role(self) -> Literal["system"]:
        """root item 恒为 wire_role=system;同一 epoch 不存在第二个 system item。"""
        return "system"

    def frame_manifest(self) -> dict[str, object]:
        """root frame 的规范 manifest;hash 算法固定,不可由调用方覆盖。"""
        return {
            "kind": "root-system-item:v1",
            "epoch": self.epoch.token,
            "provider_profile": self.provider_profile,
            "tool_compatibility_key": self.tool_compatibility_key,
            "role": self.role,
            "payload_kind": "text",
            "content": self.content,
            "sources": [
                {
                    "source_identity": provenance.source_identity,
                    "source_kind": provenance.source_kind,
                    "name": provenance.name,
                    "revision": provenance.revision,
                    "content_hash": provenance.content_hash,
                    "source_ordinal": provenance.source_ordinal,
                    "included_reason": provenance.included_reason,
                }
                for provenance in self.sources
            ],
        }

    def frame_hash(self) -> str:
        return sha256_jcs(self.frame_manifest())


def compile_root_item(
    state: PrefixEpochState,
    candidates: Sequence[RootCandidateSource],
) -> CompiledRootItem | None:
    """在新 epoch 物化时把 root_eligible 受信来源编译为唯一 root system item。

    合法边界只有两种:首次组装(initial epoch)与实际 compaction、rewind、
    Provider ToolSet hard rebase 开启的新 epoch,即传入 epoch 状态尚未
    applied。epoch 已 applied 后同 epoch 只能尾部追加,重编译必须先经
    open_rebuild_epoch 开启真正的新 epoch。候选中混入 tail_only 来源直接
    失败,保证 tail_only 永不进入 system root。没有 root_eligible 候选时
    返回 None(合法:root context 至多一个,可以为零)。
    """
    if not isinstance(state, PrefixEpochState):
        raise RootCompileBoundaryError("compile_root_item 需要 PrefixEpochState")
    if state.epoch_applied:
        raise RootCompileBoundaryError(
            f"prefix epoch {state.epoch.token} 已 applied,同 epoch 只能尾部追加;"
            "重编译 root 必须先开启 compaction/rewind/toolset_changed 新 epoch"
        )
    allowed_reasons = {EpochReason.INITIAL, *REBUILD_EPOCH_REASONS}
    if state.epoch.reason not in allowed_reasons:
        raise RootCompileBoundaryError(
            f"prefix epoch reason {state.epoch.reason!r} 不是合法 root 物化边界;"
            "不存在第五类 epoch"
        )
    for candidate in candidates:
        if not isinstance(candidate, RootCandidateSource):
            raise TypeError("compile_root_item 只接受 RootCandidateSource")
        if candidate.root_placement != "root_eligible":
            raise RootPlacementViolationError(
                f"source {candidate.source_identity!r} 声明为 "
                f"{candidate.root_placement!r},不得进入 root 编译;"
                "tail_only 只能作为独立 user-role item 追加"
            )
    eligible = sorted(
        candidates, key=lambda item: (item.source_ordinal, item.source_identity)
    )
    if not eligible:
        return None
    seen_identities: set[str] = set()
    seen_ordinals: set[int] = set()
    for candidate in eligible:
        if candidate.source_identity in seen_identities:
            raise RootCompileConflictError(
                f"root 编译候选重复 source identity: {candidate.source_identity!r}"
            )
        if candidate.source_ordinal in seen_ordinals:
            raise RootCompileConflictError(
                f"root 编译候选重复 source ordinal: {candidate.source_ordinal}"
            )
        seen_identities.add(candidate.source_identity)
        seen_ordinals.add(candidate.source_ordinal)
    return CompiledRootItem(
        epoch=state.epoch,
        provider_profile=state.provider_profile,
        tool_compatibility_key=state.tool_compatibility_key,
        content="\n\n".join(candidate.content for candidate in eligible),
        sources=tuple(
            CompiledRootSourceProvenance(
                source_identity=candidate.source_identity,
                source_kind=candidate.source_kind,
                name=candidate.name,
                revision=candidate.revision,
                content_hash=candidate.content_hash,
                source_ordinal=candidate.source_ordinal,
                included_reason=candidate.included_reason,
            )
            for candidate in eligible
        ),
    )


def resolve_source_wire_role(
    root_placement: RootPlacement,
    *,
    compiled_into_root: bool,
) -> WireRole:
    """按位置确定 CSM source item 的 wire role(E1 的 user-role projection 合同)。

    tail_only 永远是独立 user-role item,传入 compiled_into_root=True 直接
    失败;root_eligible 只有在合法新 epoch 编译进唯一 root 时才是 system。
    第一条真实用户消息之后的 full/delta/恢复一律走 compiled_into_root=False,
    按独立 user item 追加,不合并、不前插。
    """
    if root_placement not in _ROOT_PLACEMENTS:
        raise RootPlacementViolationError(
            f"未知 root_placement: {root_placement!r};"
            "资格只能由 source owner 显式声明为 root_eligible|tail_only"
        )
    if not isinstance(compiled_into_root, bool):
        raise TypeError("compiled_into_root 必须是 boolean")
    if root_placement == "tail_only":
        if compiled_into_root:
            raise RootPlacementViolationError(
                "tail_only 来源不得编译进 root,只能作为独立 user-role item"
            )
        return "user"
    return "system" if compiled_into_root else "user"


def validate_epoch_root_uniqueness(
    existing: CompiledRootItem | None,
    incoming: CompiledRootItem,
) -> None:
    """同一 prefix epoch 至多投影一个 root system item;重复编译直接失败。"""
    if existing is None:
        return
    if not isinstance(existing, CompiledRootItem) or not isinstance(
        incoming, CompiledRootItem
    ):
        raise TypeError("validate_epoch_root_uniqueness 需要 CompiledRootItem")
    if existing.epoch.token == incoming.epoch.token:
        raise RootCompileConflictError(
            f"prefix epoch {incoming.epoch.token} 已存在 root system item,"
            "不得二次编译;重编译必须先开启真正的新 epoch"
        )


__all__ = [
    "CompiledRootItem",
    "CompiledRootSourceProvenance",
    "IncludedReason",
    "RootCandidateSource",
    "RootCompileBoundaryError",
    "RootCompileConflictError",
    "RootPlacement",
    "RootPlacementViolationError",
    "WireRole",
    "compile_root_item",
    "resolve_source_wire_role",
    "validate_epoch_root_uniqueness",
]
