"""PrefixEpoch/root/ToolSet 封存合同的唯一严格 JSON 边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Literal, cast

from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    ParentAssemblyRef,
    PendingPrefixEpochTransition,
    PrefixEpoch,
    PrefixEpochState,
    ProviderProfileItemFrame,
    RuntimeNoticeAcceptanceRef,
    SealedPrefixAssembly,
    SourceTrackingAtSeal,
)
from app.domain.itemized.epoch.root_compilation import (
    CompiledRootItem,
    CompiledRootSourceProvenance,
)
from app.domain.itemized.epoch.seal_binding import (
    AppliedSourceRevision,
    ContextSealBinding,
)
from app.domain.itemized.errors import ItemSchemaError


def _object(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ItemSchemaError(f"{label} 字段缺失或含未知字段")
    return value


def _array(value: object, label: str) -> list[object] | tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ItemSchemaError(f"{label} 必须是 array")
    return value


def _epoch(value: object) -> PrefixEpoch:
    raw = _object(value, {"ordinal", "reason", "turn_id"}, "PrefixEpoch")
    return PrefixEpoch(raw["ordinal"], EpochReason(raw["reason"]), raw["turn_id"])


def _parent(value: object) -> ParentAssemblyRef | None:
    if value is None:
        return None
    raw = _object(
        value,
        {"assembly_id", "epoch_ordinal", "prefix_byte_length", "prefix_hash"},
        "ParentAssemblyRef",
    )
    return ParentAssemblyRef(**raw)


def _source_protocol(raw: Mapping[str, object]) -> Literal[1, 2] | None:
    if "source_protocol" not in raw:
        return None
    value = raw["source_protocol"]
    if type(value) is not int or value not in {1, 2}:
        raise ItemSchemaError("未知 source_protocol")
    return cast(Literal[1, 2], value)


def _item_ref(value: object, version: Literal[1, 2, 3]) -> AppendedItemRef:
    tracking_field = "tracking_state" if version == 1 else "source_tracking_at_seal"
    raw = _object(
        value,
        {"ref_type", "ref_id", "source_identity", "source_revision", tracking_field},
        "AppendedItemRef",
    )
    fields = dict(raw)
    # TODO：v1 仅服务既有封存历史；历史保留策略改变前不得删除或迁写。
    if version == 1:
        tracking = fields.pop("tracking_state")
        if tracking not in {"tracked", "not_tracked"} or (
            raw["ref_type"] == "canonical_item" and tracking != "not_tracked"
        ):
            raise ItemSchemaError("v1 AppendedItemRef.tracking_state 非法")
        fields["source_tracking_at_seal"] = (
            None if raw["ref_type"] == "canonical_item"
            else "tracked" if tracking == "tracked" else "untracked"
        )
    # 唯一领域构造器校验值域及 canonical/source 的组合；codec 不复制业务校验。
    return AppendedItemRef(
        ref_type=cast(Literal["canonical_item", "source_item"], fields["ref_type"]),
        ref_id=cast(str, fields["ref_id"]),
        source_identity=cast(str | None, fields["source_identity"]),
        source_revision=cast(str | None, fields["source_revision"]),
        source_tracking_at_seal=cast(SourceTrackingAtSeal | None, fields["source_tracking_at_seal"]),
    )


def _item_ref_to_dict(ref: AppendedItemRef, version: Literal[1, 2, 3]) -> dict[str, object]:
    fields = asdict(ref)
    # TODO：按读取时的版本复现旧封存，不能把历史引用重写为新字段。
    if version == 1:
        tracking = fields.pop("source_tracking_at_seal")
        fields["tracking_state"] = "tracked" if tracking == "tracked" else "not_tracked"
    return fields


def _state_to_dict(state: PrefixEpochState, version: Literal[1, 2, 3]) -> dict[str, object]:
    fields = state.to_dict()
    # TODO：旧版重编码只存在于此 codec，运行态和新 prepare 均使用新版引用。
    if version == 1:
        fields["frames"] = tuple(
            {**frame.frame_manifest(), "item_ref": _item_ref_to_dict(frame.item_ref, version)}
            for frame in state.frames
        )
        fields["appended_refs"] = tuple(_item_ref_to_dict(ref, version) for ref in state.appended_refs)
    return fields


def _notice_acceptance(value: object) -> RuntimeNoticeAcceptanceRef | None:
    if value is None:
        return None
    raw = _object(value, {"acceptance_commit_id", "selection_hash"}, "RuntimeNoticeAcceptanceRef")
    return RuntimeNoticeAcceptanceRef(**raw)


def context_seal_binding_to_dict(binding: ContextSealBinding) -> dict[str, object]:
    """完整封存值；不派生缺失字段，不读取资源或任何状态 owner。"""
    prefix = binding.prefix
    transition = prefix.consumed_transition
    stored_transition = asdict(transition) if transition is not None else None
    if stored_transition is not None:
        stored_transition["reason"] = transition.reason.value
    root = None
    if binding.root is not None:
        root = asdict(binding.root)
        root["epoch"]["reason"] = binding.root.epoch.reason.value
        if binding.root.source_protocol is None:
            root.pop("source_protocol")
    stored = {
        "prefix": {
            "assembly_id": prefix.assembly_id,
            "state": _state_to_dict(prefix.state, binding.format_version),
            "parent": asdict(prefix.parent) if prefix.parent is not None else None,
            "appended_refs": tuple(_item_ref_to_dict(ref, binding.format_version) for ref in prefix.appended_refs),
            "prefix_byte_length": prefix.prefix_byte_length,
            "prefix_hash": prefix.prefix_hash,
            "consumed_transition": stored_transition,
        },
        "root": root,
        "tool_set_snapshot_id": binding.tool_set_snapshot_id,
        "applied_toolset_revision": binding.applied_toolset_revision,
        "selection_revision": binding.selection_revision,
        "previous_assembly_id": binding.previous_assembly_id,
        "source_revisions": tuple(asdict(source) for source in binding.source_revisions),
        "notice_acceptance": asdict(binding.notice_acceptance) if binding.notice_acceptance is not None else None,
    }
    if binding.format_version >= 2:
        stored["format_version"] = binding.format_version
    return stored


def context_seal_binding_from_dict(payload: object) -> ContextSealBinding:
    """恢复同一 sealed frame；字段/摘要/owner 绑定漂移都直接失败。"""
    if not isinstance(payload, Mapping):
        raise ItemSchemaError("ContextSealBinding 必须是 object")
    # TODO：无版本标签唯一对应已封存的 v1；不按局部字段猜测或兼容混合格式。
    version: Literal[1, 2, 3] = 1
    if "format_version" in payload:
        if type(payload["format_version"]) is not int or payload["format_version"] not in {2, 3}:
            raise ItemSchemaError("未知 ContextSealBinding.format_version")
        version = cast(Literal[2, 3], payload["format_version"])
    raw = _object(
        payload,
        {
            "prefix", "root", "tool_set_snapshot_id", "applied_toolset_revision",
            "selection_revision", "previous_assembly_id", "source_revisions", "notice_acceptance",
        } | ({"format_version"} if version >= 2 else set()),
        "ContextSealBinding",
    )
    prefix = _object(
        raw["prefix"],
        {
            "assembly_id", "state", "parent", "appended_refs", "prefix_byte_length",
            "prefix_hash", "consumed_transition",
        },
        "SealedPrefixAssembly",
    )
    state = _object(
        prefix["state"],
        {
            "epoch", "provider_profile", "tool_compatibility_key", "epoch_applied",
            "frames", "appended_refs", "parent",
        },
        "PrefixEpochState",
    )
    frames = []
    for value in _array(state["frames"], "PrefixEpochState.frames"):
        frame = _object(
            value,
            {"ordinal", "provider_profile", "role", "payload_kind", "content", "item_ref"}
            | ({"source_protocol"} if version == 3 and isinstance(value, Mapping) and "source_protocol" in value else set()),
            "ProviderProfileItemFrame",
        )
        frames.append(ProviderProfileItemFrame(
            ordinal=frame["ordinal"], provider_profile=frame["provider_profile"],
            role=frame["role"], payload_kind=frame["payload_kind"],
            content=frame["content"], item_ref=_item_ref(frame["item_ref"], version),
            source_protocol=_source_protocol(frame),
        ))
    transition = prefix["consumed_transition"]
    if transition is not None:
        transition = _object(
            transition, {"transition_id", "reason", "turn_id", "consumed", "notice_acceptance"},
            "PendingPrefixEpochTransition",
        )
        transition = PendingPrefixEpochTransition(
            transition_id=transition["transition_id"],
            reason=EpochReason(transition["reason"]),
            turn_id=transition["turn_id"],
            consumed=transition["consumed"],
            notice_acceptance=_notice_acceptance(transition["notice_acceptance"]),
        )
    sealed_prefix = SealedPrefixAssembly(
        assembly_id=prefix["assembly_id"],
        state=PrefixEpochState(
            epoch=_epoch(state["epoch"]),
            provider_profile=state["provider_profile"],
            tool_compatibility_key=state["tool_compatibility_key"],
            epoch_applied=state["epoch_applied"],
            frames=tuple(frames),
            appended_refs=tuple(
                _item_ref(value, version) for value in _array(state["appended_refs"], "appended_refs")
            ),
            parent=_parent(state["parent"]),
        ),
        parent=_parent(prefix["parent"]),
        appended_refs=tuple(
            _item_ref(value, version) for value in _array(prefix["appended_refs"], "appended_refs")
        ),
        prefix_byte_length=prefix["prefix_byte_length"],
        prefix_hash=prefix["prefix_hash"],
        consumed_transition=transition,
    )
    root = raw["root"]
    if root is not None:
        root = _object(
            root,
            {
                "epoch", "provider_profile", "tool_compatibility_key", "content", "sources",
            } | ({"source_protocol"} if version == 3 and isinstance(root, Mapping) and "source_protocol" in root else set()),
            "CompiledRootItem",
        )
        sources = []
        for value in _array(root["sources"], "CompiledRootItem.sources"):
            source = _object(
                value,
                {
                    "source_identity", "source_kind", "name", "revision", "content_hash",
                    "source_ordinal", "included_reason",
                },
                "CompiledRootSourceProvenance",
            )
            sources.append(CompiledRootSourceProvenance(**source))
        root = CompiledRootItem(
            epoch=_epoch(root["epoch"]), provider_profile=root["provider_profile"],
            tool_compatibility_key=root["tool_compatibility_key"],
            content=root["content"], sources=tuple(sources), source_protocol=_source_protocol(root),
        )
    revisions = []
    for value in _array(raw["source_revisions"], "source_revisions"):
        revision = _object(
            value, {"source_id", "revision", "decision_id", "previous_revision", "expected_control_revision"},
            "AppliedSourceRevision",
        )
        revisions.append(AppliedSourceRevision(**revision))
    return ContextSealBinding(
        prefix=sealed_prefix, root=root,
        tool_set_snapshot_id=raw["tool_set_snapshot_id"],
        applied_toolset_revision=raw["applied_toolset_revision"],
        selection_revision=raw["selection_revision"],
        previous_assembly_id=raw["previous_assembly_id"],
        source_revisions=tuple(revisions),
        notice_acceptance=_notice_acceptance(raw["notice_acceptance"]),
        format_version=version,
    )
