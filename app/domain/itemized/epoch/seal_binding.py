"""唯一 assembly seal 的来源、ToolSet 与 PrefixEpoch 纯值绑定。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from app.domain.itemized.epoch.prefix_epoch import (
    PendingPrefixEpochTransition,
    PrefixEpochState,
    RuntimeNoticeAcceptanceRef,
    SealedPrefixAssembly,
    append_items,
    initial_epoch_state,
    seal_assembly,
    stable_prefix_bytes,
    stable_prefix_digest,
    verify_stable_prefix,
)
from app.domain.itemized.epoch.root_compilation import CompiledRootItem
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs, validate_hash_token
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class AppliedSourceRevision:
    """同一次 seal 已应用的 source 决策；没有正文或第二份控制状态。"""

    source_id: str
    revision: str
    decision_id: str
    previous_revision: str | None = None
    expected_control_revision: int | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "revision", "decision_id"):
            _non_empty_string(getattr(self, name), f"AppliedSourceRevision.{name}")
        if self.previous_revision is not None:
            _non_empty_string(self.previous_revision, "AppliedSourceRevision.previous_revision")
            if self.previous_revision == self.revision and self.expected_control_revision is None:
                raise ItemSchemaError("source seal 不得重复应用同一 revision")
        if self.expected_control_revision is not None and (
            type(self.expected_control_revision) is not int or self.expected_control_revision < 1
        ):
            raise ItemSchemaError("source rebuild 必须冻结合法 control revision")


@dataclass(frozen=True, slots=True)
class ContextSealPreparation:
    """model-call 冻结的准备值；成功的唯一 seal 才能将它变为 applied。"""

    prefix_state: PrefixEpochState
    root: CompiledRootItem | None
    tool_set: ToolSetSnapshot
    previous_assembly_id: str | None
    source_revisions: tuple[AppliedSourceRevision, ...] = ()
    transition: PendingPrefixEpochTransition | None = None
    notice_acceptance: RuntimeNoticeAcceptanceRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prefix_state, PrefixEpochState):
            raise ItemSchemaError("ContextSealPreparation.prefix_state 类型非法")
        if not isinstance(self.tool_set, ToolSetSnapshot):
            raise ItemSchemaError("ContextSealPreparation.tool_set 类型非法")
        if self.notice_acceptance is not None and not isinstance(self.notice_acceptance, RuntimeNoticeAcceptanceRef):
            raise ItemSchemaError("prepared notice acceptance 必须是 typed ref")
        if self.previous_assembly_id is not None:
            _non_empty_string(self.previous_assembly_id, "previous_assembly_id")
        if self.root is not None and (
            not isinstance(self.root, CompiledRootItem)
            or self.root.epoch != self.prefix_state.epoch
            or self.root.provider_profile != self.prefix_state.provider_profile
            or self.root.tool_compatibility_key != self.prefix_state.tool_compatibility_key
        ):
            raise ItemSchemaError("prepared root 与 prefix epoch 不一致")
        if not isinstance(self.source_revisions, tuple) or not all(
            isinstance(source, AppliedSourceRevision) for source in self.source_revisions
        ):
            raise ItemSchemaError("prepared source revisions 必须是 typed tuple")
        identities = [source.source_id for source in self.source_revisions]
        if len(identities) != len(set(identities)):
            raise ItemSchemaError("一次 prepare 不得携带同一 source 的中间 revision")

    def input_hash(self) -> str:
        """随机 assembly identity 分配前冻结完整准备 preimage。"""
        return sha256_jcs({
            "schema": "context-seal-preparation:v2",
            "prefix_state": self.prefix_state.to_dict(),
            "root": self.root.frame_manifest() if self.root is not None else None,
            "tool_set_snapshot_id": self.tool_set.snapshot_id,
            "selection_revision": self.tool_set.selection_revision,
            "previous_assembly_id": self.previous_assembly_id,
            "source_revisions": [asdict(source) for source in self.source_revisions],
            "transition": asdict(self.transition) if self.transition is not None else None,
            "notice_acceptance": asdict(self.notice_acceptance) if self.notice_acceptance is not None else None,
        })

    def bind(self, assembly_id: str) -> ContextSealBinding:
        """仅生成候选绑定；持久 owner 提交前不改变准备值或 applied 状态。"""
        return ContextSealBinding(
            prefix=seal_assembly(
                self.prefix_state, assembly_id=assembly_id, transition=self.transition,
            ),
            root=self.root,
            tool_set_snapshot_id=self.tool_set.snapshot_id,
            applied_toolset_revision=self.tool_set.revision,
            selection_revision=self.tool_set.selection_revision,
            previous_assembly_id=self.previous_assembly_id,
            source_revisions=self.source_revisions,
            notice_acceptance=self.notice_acceptance,
        )


@dataclass(frozen=True, slots=True)
class ContextSealBinding:
    """封存 assembly 的唯一 epoch/ToolSet applied 事实。

    desired 是 prepare 的瞬时输入；成功 seal 时才形成本绑定。正在执行的
    assembly 始终保留自己的 applied revision，下一次 desired 变化不能改写它。
    CSM 只根据 source_revisions 确认本次批次，不提前清 pending。
    """

    prefix: SealedPrefixAssembly
    root: CompiledRootItem | None
    tool_set_snapshot_id: str
    applied_toolset_revision: str
    selection_revision: str
    previous_assembly_id: str | None
    source_revisions: tuple[AppliedSourceRevision, ...] = ()
    notice_acceptance: RuntimeNoticeAcceptanceRef | None = None
    format_version: Literal[1, 2] = 2
    """只标识封存引用的编码版本，不改变 Provider frame 或 request hash。"""

    def __post_init__(self) -> None:
        if type(self.format_version) is not int or self.format_version not in {1, 2}:
            raise ItemSchemaError("ContextSealBinding.format_version 必须是 1 或 2")
        if not isinstance(self.prefix, SealedPrefixAssembly):
            raise ItemSchemaError("ContextSealBinding.prefix 必须是 SealedPrefixAssembly")
        if not self.prefix.state.epoch_applied:
            raise ItemSchemaError("ContextSealBinding 只能绑定成功 seal 的 epoch")
        prefix = self.prefix
        if self.notice_acceptance is not None and not isinstance(self.notice_acceptance, RuntimeNoticeAcceptanceRef):
            raise ItemSchemaError("sealed notice acceptance 必须是 typed ref")
        if prefix.consumed_transition is not None and prefix.consumed_transition.notice_acceptance != self.notice_acceptance:
            raise ItemSchemaError("sealed transition 与 acceptance 引用不一致")
        # 对恢复帧重新执行同一领域校验，避免序列化绕过追加顺序和来源冲突。
        append_items(
            initial_epoch_state(
                provider_profile=prefix.state.provider_profile,
                tool_compatibility_key=prefix.state.tool_compatibility_key,
            ),
            prefix.state.frames,
            desired_provider_profile=prefix.state.provider_profile,
            desired_tool_compatibility_key=prefix.state.tool_compatibility_key,
        )
        digest = stable_prefix_digest(stable_prefix_bytes(prefix.state.frames))
        if (prefix.prefix_hash, prefix.prefix_byte_length) != (
            digest.prefix_hash, digest.prefix_byte_length
        ):
            raise ItemSchemaError("ContextSealBinding prefix 摘要与 frames 不一致")
        state_parent = prefix.state.parent
        if state_parent is None or (
            state_parent.assembly_id != prefix.assembly_id
            or state_parent.prefix_hash != prefix.prefix_hash
            or state_parent.prefix_byte_length != prefix.prefix_byte_length
        ):
            raise ItemSchemaError("sealed prefix 缺少自己的 parent binding")
        if self.previous_assembly_id is not None:
            _non_empty_string(self.previous_assembly_id, "previous_assembly_id")
            if self.previous_assembly_id == prefix.assembly_id:
                raise ItemSchemaError("previous assembly 不得引用自身")
        if prefix.parent is not None:
            if prefix.parent.assembly_id != self.previous_assembly_id:
                raise ItemSchemaError("prefix parent 与 seal predecessor 不一致")
            verify_stable_prefix(prefix.parent, prefix.state.frames)
        elif prefix.state.epoch.ordinal > 1 and self.previous_assembly_id is None:
            raise ItemSchemaError("rebuild epoch 缺少真正的 previous assembly")
        if prefix.consumed_transition is not None and (
            not prefix.consumed_transition.consumed
            or prefix.consumed_transition.reason != prefix.state.epoch.reason
        ):
            raise ItemSchemaError("seal transition 未消费或 reason 不一致")
        _non_empty_string(self.tool_set_snapshot_id, "ContextSealBinding.tool_set_snapshot_id")
        validate_hash_token(self.applied_toolset_revision, "applied_toolset_revision")
        validate_hash_token(self.selection_revision, "selection_revision")
        if self.root is not None and (
            not isinstance(self.root, CompiledRootItem)
            or self.root.epoch != self.prefix.state.epoch
            or self.root.provider_profile != self.prefix.state.provider_profile
            or self.root.tool_compatibility_key != self.prefix.state.tool_compatibility_key
        ):
            raise ItemSchemaError("ContextSealBinding.root 与 sealed epoch 不一致")
        system_frames = tuple(frame for frame in prefix.state.frames if frame.role == "system")
        if self.root is None:
            if system_frames:
                raise ItemSchemaError("未编译 root 的 epoch 不得封存 system frame")
        elif (
            len(system_frames) != 1
            or system_frames[0] != prefix.state.frames[0]
            or system_frames[0].content != self.root.content
            or system_frames[0].item_ref.source_revision != self.root.frame_hash()
        ):
            raise ItemSchemaError("唯一 root 必须与 epoch 首个 system frame 精确绑定")
        if not isinstance(self.source_revisions, tuple) or not all(
            isinstance(source, AppliedSourceRevision) for source in self.source_revisions
        ):
            raise ItemSchemaError("ContextSealBinding.source_revisions 必须是 typed tuple")
        identities = [source.source_id for source in self.source_revisions]
        if len(identities) != len(set(identities)):
            raise ItemSchemaError("一次 seal 不得应用同一 source 的多个中间 revision")
