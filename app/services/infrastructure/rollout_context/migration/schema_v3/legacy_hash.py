"""冻结 schema2 裸 detail identity 的 hash preimage；只供显式升级验证旧件。"""

from __future__ import annotations

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.serialization import normalize_wire_request

_SOURCE = ("source_revision", "content_length", "content_hash", "redacted_stable_digest")
_OVERLAY = ("base_delta_role", "source_overlay_epoch", "overlay_from_revision", "overlay_to_revision", "overlay_diff_hash")
_ENTRY = ("plan_ordinal", "selection_kind", "included", "omission_reason", "loss", "visibility", "protection", "availability", *_SOURCE, *_OVERLAY, "contribution_ordinal", "detail_ref", "contribution_id")
_TOOL = (*_SOURCE, "tool_set_schema", "tool_set_schema_version", "tool_policy_version", "tool_policy")


def _pick(value: dict, fields: tuple[str, ...]) -> dict:
    return {field: value[field] for field in fields}


def legacy_hashes(value: dict) -> tuple[str, str]:
    """不构造半初始化 domain 对象，不把新算法的结果冒充旧 hash。"""
    selection = value["selection"]
    selected = {(entry["ref"]["ref_type"], entry["ref"]["ref_id"]) for entry in selection}
    refs = sorted((ref for ref in value["refs"] if (ref["ref_type"], ref["ref_id"]) in selected), key=lambda ref: (ref["ref_type"], ref["ref_id"]))
    selected_tools = {entry["ref"]["ref_id"] for entry in selection if entry["included"] and entry["ref"]["ref_type"] == "tool_set"}
    tools = sorted((ref for ref in value["tool_set_refs"] if ref["ref_id"] in selected_tools), key=lambda ref: ref["ref_id"])
    contribution_ids = {entry["contribution_id"] for entry in selection if entry["ref"]["ref_type"] == "request_only" and entry["contribution_id"] is not None}
    contributions = sorted((row for row in value["contributions"] if row["contribution_id"] in contribution_ids), key=lambda row: (row["contribution_ordinal"] if row["contribution_ordinal"] is not None else row["metadata"]["source_ordinal"], row["contribution_id"]))
    # owner 字段加入前后的 schema2 envelope 可以按显式字段集合分派；不能
    # 在 hash 失败后删字段/试探候选算法。混合 owner 形态由 snapshot parser 拒绝。
    owned = all("session_id" in ref for ref in (*value["refs"], *value["tool_set_refs"]))
    owner = ("session_id", "plan_id") if owned else ()
    plan_refs = [
        _pick(ref, (*owner, "ref_id", "ref_type", "semantic_kind", "payload_kind", "status", "item_sequence", *_SOURCE, *_OVERLAY))
        for ref in refs
    ]
    plan_selection = []
    for entry in selection:
        ref = entry["ref"]
        source = _pick(ref, (*owner, "ref_type", "ref_id", *_SOURCE))
        source.update({field: ref[field] if ref["ref_type"] != "tool_set" else None for field in _OVERLAY[2:]})
        plan_selection.append({**_pick(entry, _ENTRY), "ref": source})
    plan_tools = [{**_pick(ref, (*owner, *_TOOL)), "tool_set_snapshot_id": ref["ref_id"]} for ref in tools]
    plan_contributions = [{
        **_pick(row, ("contribution_ordinal", "contribution_id", "source_kind", "contribution_kind", *_SOURCE, "request_only", "visibility", "protection")),
        "source_ordinal": row["metadata"].get("source_ordinal"),
    } for row in contributions]
    plan_hash = sha256_jcs({
        "schema": "context-plan-hash:v2",
        **_pick(value, ("format_version", "active_view_id", "history_view_revision", "source_overlay_epoch", "selection_policy", "compiler_version")),
        "refs": plan_refs, "selection": plan_selection,
        "tool_set_refs": plan_tools, "contributions": plan_contributions,
    })
    wire = value["request_hash_preimage"]
    if wire is None:
        wire = {"target_format": value["target_format"], "selection": [{
            **_pick(entry, ("plan_ordinal", "selection_kind", "included", *_SOURCE, *_OVERLAY, "contribution_ordinal", "detail_ref", "contribution_id", "omission_reason", "loss")),
            **_pick(entry["ref"], ("ref_type", "ref_id")),
        } for entry in selection]}
    request_hash = sha256_jcs({
        "plan_hash": plan_hash, "provider": value["provider_version"],
        **_pick(value, ("projector_id", "projector_version", "target_format")),
        "refs": [_pick(ref, ("ref_id", "ref_type", "semantic_kind", "payload_kind", "status", *_SOURCE, *_OVERLAY)) for ref in refs],
        "contributions": [_pick(row, ("contribution_id", *_SOURCE, "contribution_ordinal")) for row in contributions],
        "tool_set_refs": [{**_pick(ref, _TOOL), "tool_set_snapshot_id": ref["ref_id"]} for ref in tools],
        "selection": [{**_pick(entry, _ENTRY), "ref": _pick(entry["ref"], ("ref_type", "ref_id", *_SOURCE))} for entry in selection],
        "tool_snapshot": [], "wire_request": normalize_wire_request(wire),
    })
    return plan_hash, request_hash
