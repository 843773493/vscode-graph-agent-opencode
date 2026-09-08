"""旧 sealed artifact 验证与 typed snapshot 映射；canonical identity 不变。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.request_hash import context_request_hash
from app.services.infrastructure.rollout_context.migration.schema_v3.legacy_hash import (
    legacy_hashes,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3UpgradeError,
    object_json,
    require_equal,
)


def load_snapshot(row: dict, session_id: str) -> dict:
    value = object_json(row["snapshot_json"], field="旧 snapshot")
    for field in ("assembly_id", "session_id", "plan_id", "turn_id", "execution_id", "model_call_id", "plan_hash", "request_hash", "history_view_revision", "source_overlay_epoch"):
        require_equal(value[field], row[field], "旧 snapshot header " + field)
    require_equal(value["session_id"], session_id, "旧 snapshot owner")
    require_equal(value["format_version"], 2, "旧 snapshot format")
    require_equal(value["sealed"], True, "旧 snapshot sealed")
    require_equal(value["plan_state"], "sealed", "旧 snapshot plan_state")
    if value["hash_algorithm"] != "sha256:jcs:v1":
        raise SchemaV3UpgradeError("source-mismatch: 旧 snapshot hash algorithm")
    legacy_refs = [*value["refs"], *value["tool_set_refs"], *(entry["ref"] for entry in value["selection"])]
    owner_shapes = {"session_id" in ref for ref in legacy_refs}
    if len(owner_shapes) > 1:
        raise SchemaV3UpgradeError("source-mismatch: 旧 snapshot 混合 owner 形态")
    for ref in legacy_refs:
        if "session_id" in ref:
            require_equal(ref["session_id"], session_id, "旧 ref session")
            require_equal(ref["plan_id"], None if ref["ref_type"] == "canonical_item" else value["plan_id"], "旧 ref plan")
    hashes = legacy_hashes(value)
    require_equal(hashes[0], value["plan_hash"], "旧 snapshot plan hash")
    require_equal(hashes[1], value["request_hash"], "旧 snapshot request hash")
    return value


def detail_purposes(assemblies: list[dict], snapshots: dict[str, dict]) -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}

    def bind(detail_id: str, purpose: tuple[str, str, str]) -> None:
        if detail_id in result and result[detail_id] != purpose:
            raise SchemaV3UpgradeError("source-mismatch: detail 同时用于不同用途/visibility")
        result[detail_id] = purpose

    for row in assemblies:
        if row["detail_ref"] is not None:
            bind(row["detail_ref"], ("assembly_snapshot", "assembly_audit", "internal"))
        for entry in snapshots[row["assembly_id"]]["selection"]:
            if entry["detail_ref"] is not None:
                if entry["included"] is not True or entry["ref"]["ref_type"] != "request_only":
                    raise SchemaV3UpgradeError("source-mismatch: 旧 final detail binding 非法")
                bind(entry["detail_ref"], ("request_source", "request_replay", entry["visibility"]))
    return result


def typed_snapshot(value: dict, detail_map: dict[str, DetailRef]) -> ContextAssemblySnapshot:
    result = deepcopy(value)
    session_id, plan_id = result["session_id"], result["plan_id"]

    def mapped(detail_id: str) -> dict[str, str]:
        if not isinstance(detail_id, str) or detail_id not in detail_map:
            raise SchemaV3UpgradeError("source-mismatch: 旧 detail identity 未注册")
        ref = detail_map[detail_id]
        ref.require_owner(session_id)
        return ref.to_dict()

    def context_ref(ref: dict, final_detail: object = None) -> dict:
        ref = dict(ref)
        ref["session_id"] = session_id
        ref["plan_id"] = None if ref["ref_type"] == "canonical_item" else plan_id
        old_final = ref.pop("detail_ref", None)
        if old_final is not None and old_final != final_detail:
            raise SchemaV3UpgradeError("source-mismatch: 旧 ContextRef detail 与 selection 不一致")
        source = ref.get("source_ref")
        if isinstance(source, str) and source in detail_map:
            ref["source_ref"] = mapped(source)
        elif isinstance(source, dict):
            raise SchemaV3UpgradeError("source-mismatch: schema2 source_ref 不能是混入的 typed object")
        return ref

    final = {(entry["ref"]["ref_type"], entry["ref"]["ref_id"]): entry["detail_ref"] for entry in result["selection"]}
    result["refs"] = [context_ref(ref, final.get((ref["ref_type"], ref["ref_id"]))) for ref in result["refs"]]
    for ref in result["tool_set_refs"]:
        ref["session_id"] = session_id
        require_equal(ref["plan_id"], plan_id, "旧 ToolSetRef plan")
        require_equal(ref["assembly_id"], result["assembly_id"], "旧 ToolSetRef assembly")
    for entry in result["selection"]:
        require_equal(entry["assembly_id"], result["assembly_id"], "旧 selection assembly")
        if entry["ref"]["ref_type"] == "tool_set":
            entry["ref"]["session_id"] = session_id
        else:
            entry["ref"] = context_ref(entry["ref"], entry["detail_ref"])
        if entry["detail_ref"] is not None:
            entry["detail_ref"] = mapped(entry["detail_ref"])
    for contribution in result["contributions"]:
        contribution["metadata"] = remap_source_metadata(contribution["metadata"], detail_map)
    # wire request preimage 是 Provider 编码事实，不做字符串替换。没有实际
    # wire preimage 时，由 domain 使用新 selection 生成默认 hash preimage。
    snapshot = ContextAssemblySnapshot.from_dict(result)
    if canonical_json_bytes(snapshot.to_dict()) != canonical_json_bytes(result):
        raise SchemaV3UpgradeError("source-mismatch: snapshot 解析丢失字段或改变 selection")
    plan = snapshot.as_sealed_plan()
    snapshot = replace(snapshot, plan_hash=plan.plan_hash(), request_hash=context_request_hash(
        plan, snapshot.provider_version, projector_id=snapshot.projector_id,
        projector_version=snapshot.projector_version, target_format=snapshot.target_format,
        wire_request=snapshot.request_hash_preimage,
    ))
    snapshot.validate_hashes()
    return snapshot


def remap_source_metadata(value: object, detail_map: dict[str, DetailRef]) -> object:
    """只映射显式 metadata 引用，不改正文/工具 schema 中恰好相等的文本。"""
    if isinstance(value, list):
        return [remap_source_metadata(child, detail_map) for child in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if key in {"lineage", "legacy_source_ref", "source_lineage", "fork_lineage"}:
            result[key] = child
        elif key in {"detail_ref", "source_ref"} and isinstance(child, str) and child in detail_map:
            result[key] = detail_map[child].to_dict()
        else:
            result[key] = remap_source_metadata(child, detail_map)
    return result
