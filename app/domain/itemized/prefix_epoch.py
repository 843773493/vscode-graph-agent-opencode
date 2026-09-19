"""Prefix epoch 与稳定前缀合同的领域定义。

本模块是 OpenSpec add-context-injection-lifecycle 任务 1.2/1.4 的 domain
owner:定义 prefix_epoch、epoch_reason、ParentAssemblyRef、
PendingPrefixEpochTransition、stable prefix byte length/hash、
Provider-profile item frame、精确 ToolSetRef/policy compatibility key 与
appended item refs。只提供纯值对象与合同校验,不读取 rollout、Provider、
CSM 或任何运行时状态。

合同要点:
- 只有 initial、compaction、rewind、toolset_changed 四种 epoch_reason,
  不存在第五类 epoch(如 provider profile 变化)。
- pending transition 没有 wire bytes 且不是 applied epoch;只有首个
  assembly 成功 seal 才应用 epoch 并消费 transition。
- 同一 epoch 内只能追加,父 assembly 前缀必须逐字节一致;阈值与 hash
  算法固定,不可由调用方覆盖。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    _ensure_json_value,
    canonical_json_bytes,
    sha256_jcs,
    validate_hash_token,
)
from app.domain.itemized.refs import ToolSetRef

_SHA256_BYTES_PATTERN = re.compile(r"^sha256:bytes:v1:[0-9a-f]{64}$")
_WIRE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_TRACKING_STATES = frozenset({"tracked", "not_tracked"})
_APPEND_REF_TYPES = frozenset({"canonical_item", "source_item"})


class EpochContractError(ItemSchemaError):
    """prefix epoch 合同被非法组合破坏。"""

    error_code = "epoch-contract-violation"


class StablePrefixViolationError(ItemSchemaError):
    """父 assembly 稳定前缀的长度、hash 或逐字节内容不一致。"""

    error_code = "stable-prefix-violation"


class ToolSetRebaseRequiredError(ItemSchemaError):
    """desired ToolSet/policy 与 applied binding 不同,必须先 hard rebase。"""

    error_code = "toolset-hard-rebase-required"


class ProviderProfileChangeRequiresRebuildError(ItemSchemaError):
    """已有 assembly 后 desired provider profile 变化,只能随合法新 epoch 原子 applied。"""

    error_code = "provider-profile-change-requires-rebuild"


class SourceIdentityConflictError(ItemSchemaError):
    """同一 source identity/revision 被贡献了不一致正文或重复 body。"""

    error_code = "source-identity-conflict"


class EpochReason(StrEnum):
    """能开启新 prefix epoch 的闭合原因集合。"""

    INITIAL = "initial"
    COMPACTION = "compaction"
    REWIND = "rewind"
    TOOLSET_CHANGED = "toolset_changed"


REBUILD_EPOCH_REASONS: frozenset[EpochReason] = frozenset(
    {
        EpochReason.COMPACTION,
        EpochReason.REWIND,
        EpochReason.TOOLSET_CHANGED,
    }
)
PENDING_TRANSITION_REASONS: frozenset[EpochReason] = frozenset(
    {EpochReason.COMPACTION, EpochReason.REWIND}
)


def _non_empty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ItemSchemaError(f"{field_name} 必须是正整数")
    return value


def _validate_epoch_reason(value: object, field_name: str) -> EpochReason:
    if not isinstance(value, EpochReason):
        raise EpochContractError(
            f"{field_name} 必须是 EpochReason 闭合集合成员,收到 {value!r};"
            "不存在第五类 epoch reason"
        )
    return value


@dataclass(frozen=True, slots=True)
class PrefixEpoch:
    """SessionThread 内单调递增的 prefix epoch 值对象。

    同一 Turn 允许多个 model-call-scoped epoch:每次合法重建都会让
    ordinal 递增,turn_id 只作 provenance,不限制 epoch 数量。
    """

    ordinal: int
    reason: EpochReason
    turn_id: str | None = None

    def __post_init__(self) -> None:
        _positive_int(self.ordinal, "PrefixEpoch.ordinal")
        _validate_epoch_reason(self.reason, "PrefixEpoch.reason")
        if self.reason == EpochReason.INITIAL and self.ordinal != 1:
            raise EpochContractError("initial epoch 的 ordinal 必须是 1")
        if self.turn_id is not None:
            _non_empty_str(self.turn_id, "PrefixEpoch.turn_id")

    @property
    def token(self) -> str:
        return f"prefix-epoch:{self.ordinal}:{self.reason.value}"


@dataclass(frozen=True, slots=True)
class ParentAssemblyRef:
    """同一 epoch 内后续 assembly 必须逐字节继承的父前缀引用。"""

    assembly_id: str
    epoch_ordinal: int
    prefix_byte_length: int
    prefix_hash: str

    def __post_init__(self) -> None:
        _non_empty_str(self.assembly_id, "ParentAssemblyRef.assembly_id")
        _positive_int(self.epoch_ordinal, "ParentAssemblyRef.epoch_ordinal")
        if (
            not isinstance(self.prefix_byte_length, int)
            or isinstance(self.prefix_byte_length, bool)
            or self.prefix_byte_length < 0
        ):
            raise ItemSchemaError("ParentAssemblyRef.prefix_byte_length 必须是非负整数")
        if (
            not isinstance(self.prefix_hash, str)
            or _SHA256_BYTES_PATTERN.fullmatch(self.prefix_hash) is None
        ):
            raise ItemSchemaError(
                "ParentAssemblyRef.prefix_hash 必须符合 sha256:bytes:v1:<64位小写hex>"
            )


@dataclass(frozen=True, slots=True)
class PendingPrefixEpochTransition:
    """rewind/compaction 预提交的 pending epoch 迁移。

    pending transition 没有 wire bytes 且不是 applied epoch;只有首个
    assembly 成功 seal 后才允许消费一次。initial 与 toolset_changed 不走
    pending transition(后者在 model-call safe boundary 直接生效)。
    """

    transition_id: str
    reason: EpochReason
    turn_id: str | None = None
    consumed: bool = False

    def __post_init__(self) -> None:
        _non_empty_str(self.transition_id, "PendingPrefixEpochTransition.transition_id")
        _validate_epoch_reason(self.reason, "PendingPrefixEpochTransition.reason")
        if self.reason not in PENDING_TRANSITION_REASONS:
            raise EpochContractError(
                "PendingPrefixEpochTransition.reason 只能是 compaction/rewind,"
                f"收到 {self.reason.value!r}"
            )
        if self.turn_id is not None:
            _non_empty_str(self.turn_id, "PendingPrefixEpochTransition.turn_id")
        if not isinstance(self.consumed, bool):
            raise ItemSchemaError("PendingPrefixEpochTransition.consumed 必须是 boolean")

    def consume(self) -> PendingPrefixEpochTransition:
        """返回已消费副本;重复消费直接失败,不允许二次应用。"""
        if self.consumed:
            raise EpochContractError(
                f"pending transition {self.transition_id!r} 已消费,"
                "只能随首个成功 seal 应用一次"
            )
        return replace(self, consumed=True)


@dataclass(frozen=True, slots=True)
class AppendedItemRef:
    """追加 item 的 typed 引用,携带 source 绑定供冲突校验。

    canonical_item 不得携带 source 字段;source_item 必须携带
    source_identity,tracked 还必须有 source_revision。not_tracked 是
    闭合 tracking 状态,不参与 revision 冲突校验。
    """

    ref_type: str
    ref_id: str
    source_identity: str | None = None
    source_revision: str | None = None
    tracking_state: str = "not_tracked"

    def __post_init__(self) -> None:
        if self.ref_type not in _APPEND_REF_TYPES:
            raise ItemSchemaError(f"未知 AppendedItemRef.ref_type: {self.ref_type}")
        _non_empty_str(self.ref_id, "AppendedItemRef.ref_id")
        if self.tracking_state not in _TRACKING_STATES:
            raise ItemSchemaError(
                f"未知 AppendedItemRef.tracking_state: {self.tracking_state}"
            )
        if self.ref_type == "canonical_item":
            if self.source_identity is not None or self.source_revision is not None:
                raise ItemSchemaError("canonical AppendedItemRef 不得携带 source 绑定")
            if self.tracking_state != "not_tracked":
                raise ItemSchemaError("canonical AppendedItemRef 不得声明 tracking 状态")
            return
        _non_empty_str(self.source_identity, "AppendedItemRef.source_identity")
        if self.tracking_state == "tracked":
            _non_empty_str(self.source_revision, "AppendedItemRef.source_revision")


@dataclass(frozen=True, slots=True)
class ProviderProfileItemFrame:
    """Provider profile 下单个 wire context item 的不可变 frame。

    frame_bytes 是 RFC 8785 JCS 无空白 UTF-8 编码,是稳定前缀的最小拼接
    单元;hash 算法固定为 sha256:jcs:v1,不可由调用方覆盖。
    """

    ordinal: int
    provider_profile: str
    role: str
    payload_kind: str
    content: object
    item_ref: AppendedItemRef

    def __post_init__(self) -> None:
        _positive_int(self.ordinal, "ProviderProfileItemFrame.ordinal")
        _non_empty_str(self.provider_profile, "ProviderProfileItemFrame.provider_profile")
        if self.role not in _WIRE_ROLES:
            raise ItemSchemaError(f"未知 wire role: {self.role}")
        if self.payload_kind not in {item.value for item in PayloadKind}:
            raise ItemSchemaError(f"未知 payload_kind: {self.payload_kind}")
        if not isinstance(self.item_ref, AppendedItemRef):
            raise ItemSchemaError("ProviderProfileItemFrame.item_ref 必须是 AppendedItemRef")
        _ensure_json_value(self.content, "ProviderProfileItemFrame.content")

    def frame_manifest(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "provider_profile": self.provider_profile,
            "role": self.role,
            "payload_kind": self.payload_kind,
            "content": self.content,
        }

    def frame_bytes(self) -> bytes:
        return canonical_json_bytes(self.frame_manifest())

    def frame_hash(self) -> str:
        return sha256_jcs(self.frame_manifest())


@dataclass(frozen=True, slots=True)
class StablePrefixDigest:
    """稳定前缀的字节长度与 hash。"""

    prefix_byte_length: int
    prefix_hash: str


@dataclass(frozen=True, slots=True)
class PrefixEpochState:
    """一个 prefix epoch 的功能性状态快照。

    frames 是当前 epoch 内按 plan ordinal 排列的全部 wire frames;parent
    指向同 epoch 上一个成功 seal 的 assembly,后续 seal 必须逐字节继承其
    前缀。epoch_applied 只有在首个 assembly 成功 seal 后才为 True,对齐
    pending transition 的应用时机。
    """

    epoch: PrefixEpoch
    provider_profile: str
    tool_compatibility_key: str
    epoch_applied: bool
    frames: tuple[ProviderProfileItemFrame, ...] = ()
    appended_refs: tuple[AppendedItemRef, ...] = ()
    parent: ParentAssemblyRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.epoch, PrefixEpoch):
            raise EpochContractError("PrefixEpochState.epoch 必须是 PrefixEpoch")
        _non_empty_str(self.provider_profile, "PrefixEpochState.provider_profile")
        validate_hash_token(
            self.tool_compatibility_key, "PrefixEpochState.tool_compatibility_key"
        )
        if not isinstance(self.epoch_applied, bool):
            raise ItemSchemaError("PrefixEpochState.epoch_applied 必须是 boolean")
        if self.parent is not None and not isinstance(self.parent, ParentAssemblyRef):
            raise EpochContractError("PrefixEpochState.parent 必须是 ParentAssemblyRef")
        if self.parent is not None and self.parent.epoch_ordinal != self.epoch.ordinal:
            raise EpochContractError("parent assembly 必须属于当前 prefix epoch")

    @property
    def prefix_byte_length(self) -> int:
        return len(stable_prefix_bytes(self.frames))

    @property
    def prefix_hash(self) -> str:
        return stable_prefix_hash(stable_prefix_bytes(self.frames))


@dataclass(frozen=True, slots=True)
class SealedPrefixAssembly:
    """一次成功 seal 的结果快照。"""

    assembly_id: str
    state: PrefixEpochState
    parent: ParentAssemblyRef | None
    appended_refs: tuple[AppendedItemRef, ...]
    prefix_byte_length: int
    prefix_hash: str
    consumed_transition: PendingPrefixEpochTransition | None = None


def stable_prefix_hash(prefix_bytes: bytes) -> str:
    """固定算法的原始字节前缀 hash:sha256:bytes:v1。"""
    if not isinstance(prefix_bytes, bytes):
        raise ItemSchemaError("prefix_bytes 必须是 bytes")
    return "sha256:bytes:v1:" + hashlib.sha256(prefix_bytes).hexdigest()


def stable_prefix_bytes(frames: Sequence[ProviderProfileItemFrame]) -> bytes:
    """按顺序拼接 frames 的 JCS 编码,得到稳定前缀字节串。"""
    for frame in frames:
        if not isinstance(frame, ProviderProfileItemFrame):
            raise ItemSchemaError("stable prefix 只能由 ProviderProfileItemFrame 组成")
    return b"".join(frame.frame_bytes() for frame in frames)


def stable_prefix_digest(prefix_bytes: bytes) -> StablePrefixDigest:
    return StablePrefixDigest(
        prefix_byte_length=len(prefix_bytes),
        prefix_hash=stable_prefix_hash(prefix_bytes),
    )


def verify_stable_prefix(
    parent: ParentAssemblyRef,
    frames: Sequence[ProviderProfileItemFrame],
) -> int:
    """校验 frames 完整覆盖并逐字节继承父前缀,返回继承 frame 数。

    父前缀必须落在 frame 边界上:不覆盖、越界拆 frame 或 hash 不一致都
    直接返回 stable-prefix violation,不以重新组装静默继续。
    """
    if not isinstance(parent, ParentAssemblyRef):
        raise ItemSchemaError("parent 必须是 ParentAssemblyRef")
    offset = 0
    inherited_count = 0
    for frame in frames:
        if offset >= parent.prefix_byte_length:
            break
        if not isinstance(frame, ProviderProfileItemFrame):
            raise ItemSchemaError("stable prefix 只能由 ProviderProfileItemFrame 组成")
        offset += len(frame.frame_bytes())
        inherited_count += 1
        if offset > parent.prefix_byte_length:
            raise StablePrefixViolationError(
                f"assembly {parent.assembly_id!r} 的父前缀长度 "
                f"{parent.prefix_byte_length} 落在 frame 边界内,出现越界 frame"
            )
    if offset != parent.prefix_byte_length:
        raise StablePrefixViolationError(
            f"assembly {parent.assembly_id!r} 的 frames 只覆盖 {offset} 字节,"
            f"不足以继承父前缀长度 {parent.prefix_byte_length}"
        )
    inherited_bytes = stable_prefix_bytes(frames[:inherited_count])
    if stable_prefix_hash(inherited_bytes) != parent.prefix_hash:
        raise StablePrefixViolationError(
            f"assembly {parent.assembly_id!r} 的父前缀 hash 不一致,"
            f"期望 {parent.prefix_hash}"
        )
    return inherited_count


def toolset_compatibility_key(tool_ref: ToolSetRef, *, policy_hash: str) -> str:
    """精确绑定 ToolSetRef manifest 与工具 policy hash 的兼容 key。

    ExtensionToolCatalog 的内部目录变化不进入该 key;只有 Provider 可见
    ToolSet manifest 与影响 root/tool visibility 的 policy hash 参与。
    """
    if not isinstance(tool_ref, ToolSetRef):
        raise ItemSchemaError("tool_ref 必须是 ToolSetRef")
    validate_hash_token(policy_hash, "policy_hash")
    manifest_token = tool_ref.content_hash
    if manifest_token is None:
        manifest_token = tool_ref.redacted_stable_digest
    return sha256_jcs(
        {
            "kind": "toolset_compatibility_key",
            "tool_set_hash": manifest_token,
            "tool_set_schema": tool_ref.tool_set_schema,
            "tool_set_schema_version": tool_ref.tool_set_schema_version,
            "tool_policy_version": tool_ref.tool_policy_version,
            "policy_hash": policy_hash,
        }
    )


def _check_source_conflicts(frames: Sequence[ProviderProfileItemFrame]) -> None:
    """同一 prefix epoch 内同一 tracked source revision 至多一个 body。"""
    seen: dict[tuple[str, str], str] = {}
    for frame in frames:
        ref = frame.item_ref
        if ref.ref_type != "source_item" or ref.tracking_state != "tracked":
            continue
        key = (ref.source_identity or "", ref.source_revision or "")
        prior_hash = seen.get(key)
        if prior_hash is None:
            seen[key] = frame.frame_hash()
            continue
        if prior_hash != frame.frame_hash():
            raise SourceIdentityConflictError(
                f"source identity {key[0]!r} revision {key[1]!r} 被以不一致正文"
                "重复贡献,返回 source identity conflict"
            )
        raise SourceIdentityConflictError(
            f"source identity {key[0]!r} revision {key[1]!r} 在同一 prefix epoch"
            "内被重复选择"
        )


def initial_epoch_state(
    *,
    provider_profile: str,
    tool_compatibility_key: str,
    turn_id: str | None = None,
) -> PrefixEpochState:
    """首次组装的 initial epoch;首个 assembly seal 成功后才 applied。"""
    return PrefixEpochState(
        epoch=PrefixEpoch(ordinal=1, reason=EpochReason.INITIAL, turn_id=turn_id),
        provider_profile=provider_profile,
        tool_compatibility_key=tool_compatibility_key,
        epoch_applied=False,
    )


def open_rebuild_epoch(
    state: PrefixEpochState,
    *,
    reason: EpochReason,
    provider_profile: str,
    tool_compatibility_key: str,
    transition: PendingPrefixEpochTransition | None = None,
    turn_id: str | None = None,
) -> PrefixEpochState:
    """在合法重建边界开启新 epoch,返回尚未 applied 的 pending 状态。

    desired provider profile 与 toolset compatibility key 在此处原子
    applied;compaction/rewind 必须携带匹配且未消费的 pending transition,
    toolset_changed 必须是有效变化且不接受 pending transition。
    """
    if not isinstance(state, PrefixEpochState):
        raise EpochContractError("open_rebuild_epoch 需要当前 PrefixEpochState")
    _validate_epoch_reason(reason, "epoch_reason")
    if reason not in REBUILD_EPOCH_REASONS:
        raise EpochContractError(
            f"epoch_reason={reason.value!r} 不是合法重建边界;"
            "只有首次组装、compaction、rewind 与 ToolSet hard rebase 能开新 epoch"
        )
    if reason in PENDING_TRANSITION_REASONS:
        if not isinstance(transition, PendingPrefixEpochTransition):
            raise EpochContractError(
                f"epoch_reason={reason.value} 必须携带未消费的 "
                "PendingPrefixEpochTransition"
            )
        if transition.reason != reason:
            raise EpochContractError(
                f"pending transition reason {transition.reason.value!r} 与 "
                f"epoch reason {reason.value!r} 不一致"
            )
        if transition.consumed:
            raise EpochContractError(
                f"pending transition {transition.transition_id!r} 已消费,不能重复应用"
            )
    elif transition is not None:
        raise EpochContractError(
            "toolset_changed 是 safe boundary 直接生效,不接受 pending transition"
        )
    if reason == EpochReason.TOOLSET_CHANGED and (
        tool_compatibility_key == state.tool_compatibility_key
    ):
        raise EpochContractError("no-op ToolSet 变化不得伪造 hard rebase")
    return PrefixEpochState(
        epoch=PrefixEpoch(ordinal=state.epoch.ordinal + 1, reason=reason, turn_id=turn_id),
        provider_profile=provider_profile,
        tool_compatibility_key=tool_compatibility_key,
        epoch_applied=False,
    )


def append_items(
    state: PrefixEpochState,
    frames: Sequence[ProviderProfileItemFrame],
    *,
    desired_provider_profile: str,
    desired_tool_compatibility_key: str,
) -> PrefixEpochState:
    """在同一 epoch 内追加 frames;desired 与 applied 不一致时显式失败。

    desired profile 变化返回 provider-profile-change-requires-rebuild,
    desired ToolSet/policy 变化返回 toolset-hard-rebase-required;两者都
    不允许静默沿用或形成第五类 epoch。
    """
    if not isinstance(state, PrefixEpochState):
        raise EpochContractError("append_items 需要当前 PrefixEpochState")
    _non_empty_str(desired_provider_profile, "desired_provider_profile")
    validate_hash_token(desired_tool_compatibility_key, "desired_tool_compatibility_key")
    if desired_provider_profile != state.provider_profile:
        raise ProviderProfileChangeRequiresRebuildError(
            f"desired provider profile {desired_provider_profile!r} 与 applied "
            f"{state.provider_profile!r} 不一致;已有 assembly 后只能返回 "
            "provider-profile-change-requires-rebuild,在实际 compaction/rewind/"
            "有效 ToolSet hard rebase 的新 epoch 原子 applied"
        )
    if desired_tool_compatibility_key != state.tool_compatibility_key:
        raise ToolSetRebaseRequiredError(
            "desired ToolSetRef/policy hash 与当前 applied binding 不一致,"
            "必须先执行 ToolSet hard rebase,不得在旧 epoch 继续 seal"
        )
    max_ordinal = max((frame.ordinal for frame in state.frames), default=0)
    new_frames = tuple(frames)
    for frame in new_frames:
        if not isinstance(frame, ProviderProfileItemFrame):
            raise ItemSchemaError("只能追加 ProviderProfileItemFrame")
        if frame.provider_profile != state.provider_profile:
            raise EpochContractError(
                f"frame ordinal={frame.ordinal} 的 provider_profile "
                f"{frame.provider_profile!r} 与当前 epoch 绑定的 "
                f"{state.provider_profile!r} 不一致"
            )
        if frame.ordinal <= max_ordinal:
            raise EpochContractError(
                f"frame ordinal 必须严格递增: {frame.ordinal} <= {max_ordinal}"
            )
        max_ordinal = frame.ordinal
    combined = state.frames + new_frames
    _check_source_conflicts(combined)
    return replace(
        state,
        frames=combined,
        appended_refs=state.appended_refs + tuple(frame.item_ref for frame in new_frames),
    )


def seal_assembly(
    state: PrefixEpochState,
    *,
    assembly_id: str,
    transition: PendingPrefixEpochTransition | None = None,
) -> SealedPrefixAssembly:
    """校验父前缀并发布父引用;首个 seal 才应用 epoch 并消费 transition。

    任何校验失败都不消费 pending transition、不应用 epoch,调用方保留
    已提交 view/transition 原样重试,不产生半成品 dispatch。
    """
    _non_empty_str(assembly_id, "assembly_id")
    if not isinstance(state, PrefixEpochState):
        raise EpochContractError("seal_assembly 需要当前 PrefixEpochState")
    if state.epoch_applied:
        if transition is not None:
            raise EpochContractError(
                "已 applied epoch 的后续 assembly 不再消费 pending transition"
            )
    else:
        if state.epoch.reason in PENDING_TRANSITION_REASONS:
            if not isinstance(transition, PendingPrefixEpochTransition):
                raise EpochContractError(
                    f"epoch_reason={state.epoch.reason.value} 的首个 seal 必须携带"
                    "匹配的 PendingPrefixEpochTransition"
                )
            if transition.reason != state.epoch.reason:
                raise EpochContractError(
                    f"pending transition reason {transition.reason.value!r} 与 "
                    f"epoch reason {state.epoch.reason.value!r} 不一致"
                )
            if transition.consumed:
                raise EpochContractError(
                    f"pending transition {transition.transition_id!r} 已消费,"
                    "不能重复应用"
                )
        elif transition is not None:
            raise EpochContractError(
                "initial/toolset_changed epoch 的 seal 不接受 pending transition"
            )
    if state.parent is not None:
        verify_stable_prefix(state.parent, state.frames)
    digest = stable_prefix_digest(stable_prefix_bytes(state.frames))
    consumed_transition = (
        transition.consume() if not state.epoch_applied and transition is not None else None
    )
    sealed_state = replace(
        state,
        epoch_applied=True,
        parent=ParentAssemblyRef(
            assembly_id=assembly_id,
            epoch_ordinal=state.epoch.ordinal,
            prefix_byte_length=digest.prefix_byte_length,
            prefix_hash=digest.prefix_hash,
        ),
        appended_refs=(),
    )
    return SealedPrefixAssembly(
        assembly_id=assembly_id,
        state=sealed_state,
        parent=state.parent,
        appended_refs=state.appended_refs,
        prefix_byte_length=digest.prefix_byte_length,
        prefix_hash=digest.prefix_hash,
        consumed_transition=consumed_transition,
    )
