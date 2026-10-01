"""unsealed plan 的严格恢复边界；不补造 assembly、selection 或 source 正文。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serde.registry import (
    _optional_string,
    _required_non_negative_int,
    _required_string,
    parse_context_ref,
    parse_contribution,
    parse_tool_set_ref,
)


def unsealed_context_plan_from_dict(value: object) -> ContextRequestPlan:
    """只接受完整 draft manifest；sealed plan 必须经 assembly owner 恢复。"""
    if not isinstance(value, Mapping):
        raise ItemSchemaError("ContextRequestPlan 必须是 object")
    fields = {
        "session_id", "format_version", "plan_id", "plan_state", "assembly_id",
        "history_view_revision", "source_overlay_epoch", "active_view_id",
        "selection_policy", "refs", "tool_set_refs", "contributions", "selection",
        "compiler_version", "plan_creation_idempotency_key", "plan_hash",
    }
    if set(value) != fields:
        raise ItemSchemaError("ContextRequestPlan draft manifest 字段不完整或包含未知字段")
    if type(value["format_version"]) is not int or value["format_version"] != 2:
        raise FormatDispatchError("ContextRequestPlan 只支持 v2 format_version")
    if value["plan_state"] != "unsealed" or value["assembly_id"] is not None:
        raise ItemSchemaError("unsealed plan 不得带 assembly binding")
    for field in ("refs", "tool_set_refs", "contributions", "selection"):
        if not isinstance(value[field], (list, tuple)):
            raise ItemSchemaError(f"ContextRequestPlan.{field} 必须是 array")
    if value["selection"]:
        raise ItemSchemaError("unsealed plan 不得带 selection")
    if not all(isinstance(ref, Mapping) for ref in value["refs"]):
        raise ItemSchemaError("ContextRequestPlan.refs 元素非法")
    plan = ContextRequestPlan(
        session_id=_required_string(value["session_id"], "ContextRequestPlan.session_id"),
        plan_id=_required_string(value["plan_id"], "ContextRequestPlan.plan_id"),
        refs=tuple(parse_context_ref(ref) for ref in value["refs"]),
        contributions=tuple(
            parse_contribution(raw, sealed=False) for raw in value["contributions"]
        ),
        tool_set_refs=tuple(parse_tool_set_ref(raw) for raw in value["tool_set_refs"]),
        history_view_revision=_required_non_negative_int(
            value["history_view_revision"], "ContextRequestPlan.history_view_revision"
        ),
        source_overlay_epoch=_required_non_negative_int(
            value["source_overlay_epoch"], "ContextRequestPlan.source_overlay_epoch"
        ),
        compiler_version=_required_string(value["compiler_version"], "compiler_version"),
        active_view_id=_optional_string(value["active_view_id"], "active_view_id"),
        selection_policy=_required_string(value["selection_policy"], "selection_policy"),
        plan_creation_idempotency_key=_optional_string(
            value["plan_creation_idempotency_key"], "plan_creation_idempotency_key"
        ),
    )
    if plan.plan_hash() != _required_string(value["plan_hash"], "plan_hash"):
        raise ItemSchemaError("plan-hash-mismatch: unsealed registry 与 plan hash 不一致")
    return plan
