"""PrefixEpoch/root/ToolSet 封存合同的唯一严格 JSON 边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    ParentAssemblyRef,
    PendingPrefixEpochTransition,
    PrefixEpoch,
    PrefixEpochState,
    ProviderProfileItemFrame,
    SealedPrefixAssembly,
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


def _item_ref(value: object) -> AppendedItemRef:
    raw = _object(
        value,
        {"ref_type", "ref_id", "source_identity", "source_revision", "tracking_state"},
        "AppendedItemRef",
    )
    return AppendedItemRef(**raw)


def context_seal_binding_to_dict(binding: ContextSealBinding) -> dict[str, object]:
    """完整封存值；不派生缺失字段，不读取资源或任何状态 owner。"""
    result = asdict(binding)
    prefix = result["prefix"]
    prefix["state"]["epoch"]["reason"] = binding.prefix.state.epoch.reason.value
    transition = binding.prefix.consumed_transition
    if transition is not None:
        prefix["consumed_transition"]["reason"] = transition.reason.value
    if binding.root is not None:
        result["root"]["epoch"]["reason"] = binding.root.epoch.reason.value
    return result


def context_seal_binding_from_dict(payload: object) -> ContextSealBinding:
    """恢复同一 sealed frame；字段/摘要/owner 绑定漂移都直接失败。"""
    raw = _object(
        payload,
        {
            "prefix", "root", "tool_set_snapshot_id", "applied_toolset_revision",
            "selection_revision", "previous_assembly_id", "source_revisions",
        },
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
            {"ordinal", "provider_profile", "role", "payload_kind", "content", "item_ref"},
            "ProviderProfileItemFrame",
        )
        frames.append(ProviderProfileItemFrame(
            ordinal=frame["ordinal"], provider_profile=frame["provider_profile"],
            role=frame["role"], payload_kind=frame["payload_kind"],
            content=frame["content"], item_ref=_item_ref(frame["item_ref"]),
        ))
    transition = prefix["consumed_transition"]
    if transition is not None:
        transition = _object(
            transition, {"transition_id", "reason", "turn_id", "consumed"},
            "PendingPrefixEpochTransition",
        )
        transition = PendingPrefixEpochTransition(
            transition_id=transition["transition_id"],
            reason=EpochReason(transition["reason"]),
            turn_id=transition["turn_id"],
            consumed=transition["consumed"],
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
                _item_ref(value) for value in _array(state["appended_refs"], "appended_refs")
            ),
            parent=_parent(state["parent"]),
        ),
        parent=_parent(prefix["parent"]),
        appended_refs=tuple(
            _item_ref(value) for value in _array(prefix["appended_refs"], "appended_refs")
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
            },
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
            content=root["content"], sources=tuple(sources),
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
    )
