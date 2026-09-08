"""v2 sealed assembly snapshot deserialization owner."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.registry import (
    _optional_non_negative_int,
    _optional_string,
    _required_bool,
    _required_non_negative_int,
    _required_string,
    parse_context_ref,
    parse_contribution,
    parse_tool_set_ref,
)

if TYPE_CHECKING:
    from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot


def context_assembly_snapshot_from_dict(
    cls: type[ContextAssemblySnapshot], value: Mapping[str, object]
) -> ContextAssemblySnapshot:
    """从 SQLite snapshot_json 恢复不可变 assembly 视图。"""
    if type(value.get("format_version")) is not int or value["format_version"] != 2:
        raise FormatDispatchError("ContextAssemblySnapshot 只支持 v2 format_version")
    required = {
        "assembly_id",
        "session_id",
        "turn_id",
        "execution_id",
        "plan_id",
        "plan_hash",
        "request_hash",
        "history_view_revision",
        "source_overlay_epoch",
        "refs",
        "contributions",
        "tool_snapshot",
        "compiler_version",
        "provider_version",
        "projector_id",
        "projector_version",
        "target_format",
        "request_hash_preimage",
        "active_view_id",
        "selection_policy",
        "model_call_id",
        "hash_algorithm",
        "loss",
        "sealed",
        "plan_state",
        "tool_set_refs",
        "selection",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ItemSchemaError(f"ContextAssemblySnapshot 缺少字段: {','.join(missing)}")
    raw_refs = value["refs"]
    raw_contributions = value["contributions"]
    raw_tools = value["tool_snapshot"]
    if not isinstance(raw_refs, (list, tuple)):
        raise ItemSchemaError("ContextAssemblySnapshot.refs 必须是 array")
    if not isinstance(raw_contributions, (list, tuple)):
        raise ItemSchemaError("ContextAssemblySnapshot.contributions 必须是 array")
    if not isinstance(raw_tools, (list, tuple)):
        raise ItemSchemaError("ContextAssemblySnapshot.tool_snapshot 必须是 array")
    if not all(isinstance(tool, Mapping) for tool in raw_tools):
        raise ItemSchemaError("ContextAssemblySnapshot.tool_snapshot 元素非法")

    refs = tuple(parse_context_ref(ref) for ref in raw_refs if isinstance(ref, Mapping))
    if len(refs) != len(raw_refs):
        raise ItemSchemaError("ContextAssemblySnapshot.refs 元素非法")

    contributions = tuple(parse_contribution(raw) for raw in raw_contributions)
    if len(contributions) != len(raw_contributions):
        raise ItemSchemaError("ContextAssemblySnapshot.contributions 元素非法")
    loss = value["loss"]
    if not isinstance(loss, (list, tuple)) or not all(
        isinstance(item, str) for item in loss
    ):
        raise ItemSchemaError("ContextAssemblySnapshot.loss 必须是字符串数组")
    raw_tool_refs = value["tool_set_refs"]
    if not isinstance(raw_tool_refs, (list, tuple)):
        raise ItemSchemaError("ContextAssemblySnapshot.tool_set_refs 必须是 array")
    tool_refs = tuple(parse_tool_set_ref(raw) for raw in raw_tool_refs)
    raw_selection = value["selection"]
    if not isinstance(raw_selection, (list, tuple)):
        raise ItemSchemaError("ContextAssemblySnapshot.selection 必须是 array")
    parsed_selection: list[ContextSelectionEntry] = []
    for raw_entry in raw_selection:
        if not isinstance(raw_entry, Mapping) or not isinstance(
            raw_entry.get("ref"), Mapping
        ):
            raise ItemSchemaError("ContextSelectionEntry 结构非法")
        if set(raw_entry) & {"ref_kind", "request_only"}:
            raise FormatDispatchError("ContextSelectionEntry 禁止 ref_kind/request_only alias")
        if type(raw_entry.get("format_version")) is not int or raw_entry["format_version"] != 2:
            raise ItemSchemaError("ContextSelectionEntry.format_version 必须为 2")
        missing_selection_fields = sorted(
            {
                "assembly_id",
                "plan_ordinal",
                "ref",
                "selection_kind",
                "included",
                "omission_reason",
                "loss",
                "visibility",
                "protection",
                "availability",
                "source_revision",
                "content_length",
                "content_hash",
                "redacted_stable_digest",
                "base_delta_role",
                "source_overlay_epoch",
                "overlay_from_revision",
                "overlay_to_revision",
                "overlay_diff_hash",
                "contribution_ordinal",
                "detail_ref",
                "contribution_id",
            }
            - set(raw_entry)
        )
        if missing_selection_fields:
            raise ItemSchemaError(
                "ContextSelectionEntry 缺少字段: " + ",".join(missing_selection_fields)
            )
        if not isinstance(raw_entry["included"], bool):
            raise ItemSchemaError("ContextSelectionEntry.included 必须是 boolean")
        if not isinstance(raw_entry["loss"], (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_entry["loss"]
        ):
            raise ItemSchemaError("ContextSelectionEntry.loss 必须是字符串数组")
        raw_ref = raw_entry["ref"]
        if raw_ref.get("ref_type") == "tool_set" and not raw_entry["included"]:
            # 此分支只解析 selection 自带的 identity，不能查工具 registry 或正文。
            identity_fields = {
                "session_id",
                "ref_type", "ref_id", "plan_id", "assembly_id", "source_revision",
                "tool_set_schema", "tool_set_schema_version", "tool_policy_version",
                "content_length", "content_hash", "redacted_stable_digest",
                "protection", "availability",
            }
            if set(raw_ref) != identity_fields:
                raise ItemSchemaError("omitted ToolSetRef identity 字段不完整或包含正文")
            selection_ref = ToolSetRef(
                **{key: raw_ref[key] for key in identity_fields}
            )
        else:
            selection_ref = (
                next(
                    (item for item in tool_refs if item.ref_id == raw_ref.get("ref_id")),
                    None,
                )
                if raw_ref.get("ref_type") == "tool_set"
                else parse_context_ref(raw_ref)
            )
        if selection_ref is None:
            raise ItemSchemaError("selection ToolSetRef 不存在")
        if (
            isinstance(selection_ref, ToolSetRef)
            and raw_entry["included"]
            and dict(raw_ref) != selection_ref.to_dict()
        ):
            raise ItemSchemaError("selection ToolSetRef manifest 与 registry 不一致")
        parsed_selection.append(
            ContextSelectionEntry(
                assembly_id=_required_string(
                    raw_entry["assembly_id"], "ContextSelectionEntry.assembly_id"
                ),
                plan_ordinal=_required_non_negative_int(
                    raw_entry["plan_ordinal"], "ContextSelectionEntry.plan_ordinal"
                ),
                ref=selection_ref,
                selection_kind=_required_string(
                    raw_entry["selection_kind"],
                    "ContextSelectionEntry.selection_kind",
                ),
                included=raw_entry["included"],
                omission_reason=(
                    _optional_string(
                        raw_entry["omission_reason"],
                        "ContextSelectionEntry.omission_reason",
                    )
                    if raw_entry.get("omission_reason") is not None
                    else None
                ),
                loss=tuple(raw_entry["loss"]),
                visibility=_required_string(
                    raw_entry["visibility"], "ContextSelectionEntry.visibility"
                ),
                protection=_required_string(
                    raw_entry["protection"], "ContextSelectionEntry.protection"
                ),
                availability=_required_string(
                    raw_entry["availability"], "ContextSelectionEntry.availability"
                ),
                source_revision=(
                    _optional_string(
                        raw_entry["source_revision"],
                        "ContextSelectionEntry.source_revision",
                    )
                    if raw_entry.get("source_revision") is not None
                    else None
                ),
                content_length=_optional_non_negative_int(
                    raw_entry.get("content_length"),
                    "ContextSelectionEntry.content_length",
                ),
                # selection entry 的完整性字段属于 entry 自身。尤其是
                # optional omitted entry，ref manifest 仍可有正文 hash，但
                # 这里不能把它回填成“已包含正文”，否则恢复会改变
                # included/loss 语义并绕过 omission gate。
                content_hash=(
                    _optional_string(
                        raw_entry["content_hash"],
                        "ContextSelectionEntry.content_hash",
                    )
                    if raw_entry.get("content_hash") is not None
                    else None
                ),
                redacted_stable_digest=(
                    _optional_string(
                        raw_entry["redacted_stable_digest"],
                        "ContextSelectionEntry.redacted_stable_digest",
                    )
                    if raw_entry.get("redacted_stable_digest") is not None
                    else None
                ),
                base_delta_role=_required_string(
                    raw_entry["base_delta_role"],
                    "ContextSelectionEntry.base_delta_role",
                ),
                source_overlay_epoch=_optional_non_negative_int(
                    raw_entry.get("source_overlay_epoch"),
                    "ContextSelectionEntry.source_overlay_epoch",
                ),
                overlay_from_revision=_optional_string(
                    raw_entry.get("overlay_from_revision"),
                    "ContextSelectionEntry.overlay_from_revision",
                ),
                overlay_to_revision=_optional_string(
                    raw_entry.get("overlay_to_revision"),
                    "ContextSelectionEntry.overlay_to_revision",
                ),
                overlay_diff_hash=_optional_string(
                    raw_entry.get("overlay_diff_hash"),
                    "ContextSelectionEntry.overlay_diff_hash",
                ),
                contribution_ordinal=_optional_non_negative_int(
                    raw_entry.get("contribution_ordinal"),
                    "ContextSelectionEntry.contribution_ordinal",
                ),
                detail_ref=(
                    DetailRef.from_dict(raw_entry["detail_ref"])
                    if raw_entry["detail_ref"] is not None else None
                ),
                contribution_id=_optional_string(
                    raw_entry.get("contribution_id"),
                    "ContextSelectionEntry.contribution_id",
                ),
            )
        )
    return cls(
        assembly_id=_required_string(value["assembly_id"], "assembly_id"),
        session_id=_required_string(value["session_id"], "session_id"),
        turn_id=_required_string(value["turn_id"], "turn_id"),
        execution_id=_required_string(value["execution_id"], "execution_id"),
        plan_id=_required_string(value["plan_id"], "plan_id"),
        plan_hash=_required_string(value["plan_hash"], "plan_hash"),
        request_hash=_required_string(value["request_hash"], "request_hash"),
        history_view_revision=_required_non_negative_int(
            value["history_view_revision"], "history_view_revision"
        ),
        source_overlay_epoch=_required_non_negative_int(
            value["source_overlay_epoch"], "source_overlay_epoch"
        ),
        refs=refs,
        contributions=contributions,
        tool_snapshot=tuple(dict(tool) for tool in raw_tools),
        compiler_version=_required_string(
            value["compiler_version"], "compiler_version"
        ),
        provider_version=_required_string(
            value["provider_version"], "provider_version"
        ),
        projector_id=_required_string(value["projector_id"], "projector_id"),
        projector_version=_required_string(
            value["projector_version"], "projector_version"
        ),
        target_format=_required_string(value["target_format"], "target_format"),
        request_hash_preimage=value["request_hash_preimage"],
        active_view_id=(
            _optional_string(value["active_view_id"], "active_view_id")
            if value["active_view_id"] is not None
            else None
        ),
        selection_policy=_required_string(
            value["selection_policy"], "selection_policy"
        ),
        model_call_id=(
            _optional_string(value["model_call_id"], "model_call_id")
            if value["model_call_id"] is not None
            else None
        ),
        hash_algorithm=_required_string(value["hash_algorithm"], "hash_algorithm"),
        loss=tuple(loss),
        sealed=_required_bool(value["sealed"], "sealed"),
        tool_set_refs=tool_refs,
        selection=tuple(parsed_selection),
        plan_state=_required_string(value["plan_state"], "plan_state"),
    )
