"""plan registry 的无正文 manifest 和精确幂等 preimage。"""

from __future__ import annotations

import re
from dataclasses import fields, replace
from hashlib import sha256
from typing import cast

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    canonical_json_bytes_and_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.refs import (
    ToolSetIdentityRef,
    ToolSetRef,
    selection_ref_identity,
)
from app.domain.itemized.plans.contributions import resolve_contribution_for_ref
from app.domain.itemized.plans.request import ContextRequestPlan


def draft_manifest(plan: ContextRequestPlan) -> dict[str, object]:
    """registry 只保存 source manifest；正文仍通过 source/detail owner 恢复。"""
    if plan.plan_state != "unsealed" or plan.assembly_id is not None or plan.selection:
        raise ValueError("plan-order-integrity: plan registry 只接收 unsealed draft")
    if not plan.plan_creation_idempotency_key:
        raise ValueError("plan-idempotency-conflict: 创建 draft 必须有显式幂等键")
    # 领域载体已隔离嵌套 JSON；本次投影不再序列化后反解析同一份 draft。
    manifest = plan.to_dict()
    for contribution in cast(list[dict[str, object]], manifest["contributions"]):
        contribution["body"] = None
    return manifest


def creation_hash(plan: ContextRequestPlan) -> str:
    """创建摘要排除顶层 plan_id/plan_hash 与 refs/tool_set_refs 中的 plan_id。

    session_id/thread_id、source/ref identity 与顺序仍参与绑定。
    """
    return _creation_hash_manifest(draft_manifest(plan))


def _creation_hash_manifest(manifest: dict[str, object]) -> str:
    """只消费同次完整 domain 校验后的 manifest，不重建 plan 或保存缓存。"""
    projection = {
        key: value for key, value in manifest.items() if key not in {"plan_id", "plan_hash"}
    }
    for field in ("refs", "tool_set_refs"):
        projection[field] = [
            {key: value for key, value in ref.items() if key != "plan_id"}
            for ref in cast(list[dict[str, object]], manifest[field])
        ]
    return sha256_jcs({"schema": "context-plan-creation:v1", "input": projection})


def validate_sealed_plan(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> None:
    """只能将当前 draft 按给定 selection 封存，不能换入另一个 source registry。"""
    _validate_contribution_bindings(draft, snapshot)
    draft = _included_tool_draft(draft, snapshot)
    snapshot.validate_hashes()
    expected = draft.seal_for_assembly(
        snapshot.assembly_id, selection=snapshot.selection
    )
    included_tools = {
        (entry.ref.ref_type, entry.ref.ref_id)
        for entry in snapshot.selection
        if entry.included and isinstance(entry.ref, ToolSetRef)
    }
    # snapshot 已通过完整 domain 校验；逐字段比较同一份 sealed manifest，
    # 不再为比较重建多个相同 plan/snapshot 并重复计算完整 ToolSet hash。
    # creation key 只属于 draft 生命周期；format_version 在两侧固定为 2。
    for field in fields(ContextRequestPlan):
        if field.name in {"selection", "tool_snapshot", "plan_creation_idempotency_key"}:
            continue
        expected_value = getattr(expected, field.name)
        actual_value = 2 if field.name == "format_version" else getattr(snapshot, field.name)
        if field.name == "tool_set_refs":
            actual_value = tuple(sorted(
                (ref for ref in snapshot.tool_set_refs
                 if (ref.ref_type, ref.ref_id) in included_tools),
                key=lambda ref: (ref.ref_type, ref.ref_id),
            ))
        if field.name in {"refs", "tool_set_refs"}:
            expected_value = [item.to_dict() for item in expected_value]
            actual_value = [item.to_dict() for item in actual_value]
        elif field.name == "contributions":
            expected_value = [{**item.to_dict(), "body": None} for item in expected_value]
            actual_value = [{**item.to_dict(), "body": None} for item in actual_value]
        if canonical_json_bytes(expected_value) != canonical_json_bytes(actual_value):
            raise ValueError(
                "plan-order-integrity: sealed snapshot 与当前 draft registry 不一致"
            )


def _validate_contribution_bindings(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> None:
    """omitted 可保留已存在的映射，但不能借跳过正文校验补造贡献身份。"""
    sources = {selection_ref_identity(ref): ref for ref in draft.refs}
    for entry in snapshot.selection:
        if entry.contribution_id is None:
            continue
        source = sources.get(selection_ref_identity(entry.ref))
        if source is None or source.ref_type != "request_only":
            raise ValueError("plan-order-integrity: contribution binding 缺少 request-only source")
        contribution = resolve_contribution_for_ref(source, draft.contributions)
        if contribution is None or contribution.contribution_id != entry.contribution_id:
            raise ValueError("plan-order-integrity: contribution binding 与 plan registry 不一致")
        for field in ("source_revision", "content_length", "content_hash"):
            value = getattr(entry, field)
            if value is not None and value != getattr(contribution, field):
                raise ValueError("source-mismatch: contribution binding manifest 不一致")


def _tool_identity(ref: ToolSetIdentityRef, assembly_id: str) -> dict[str, object]:
    # 不经 to_dict 再删正文：omitted tools/policy 可能已经不可读取。
    return {
        **ToolSetIdentityRef.to_dict(ref),
        "assembly_id": assembly_id,
    }


def _included_tool_draft(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> ContextRequestPlan:
    sources = {(ref.ref_type, ref.ref_id): ref for ref in draft.tool_set_refs}
    for ref in (
        *snapshot.tool_set_refs,
        *(
            entry.ref
            for entry in snapshot.selection
            if isinstance(entry.ref, ToolSetIdentityRef)
        ),
    ):
        source = sources.get((ref.ref_type, ref.ref_id))
        if source is None or _tool_identity(
            source, snapshot.assembly_id
        ) != _tool_identity(ref, ref.assembly_id):
            raise ValueError(
                "plan-order-integrity: ToolSetRef 与 draft source manifest 不一致"
            )
    included = {
        (entry.ref.ref_type, entry.ref.ref_id)
        for entry in snapshot.selection
        if entry.included and isinstance(entry.ref, ToolSetRef)
    }
    # 不改动持久 draft，也不从 snapshot 反向补造 source；included 正文仍由
    # 原始 draft 参与 domain manifest 校验与逐字段比较，selection 原序不变。
    return replace(draft, tool_set_refs=tuple(sources[key] for key in sorted(included)))


def _require_seal_input_hash(input_hash: str) -> None:
    if not isinstance(input_hash, str) or re.fullmatch(r"sha256:jcs:v1:[0-9a-f]{64}", input_hash) is None:
        raise ValueError("assembly-idempotency-conflict: seal_input_hash 必须是规范 JCS SHA-256 摘要")


def _runtime_seal_hash(sealed_hash: str, input_hash: str) -> str:
    return sha256_jcs({
        "schema": "context-plan-runtime-seal:v1",
        "seal_input_hash": input_hash,
        "sealed_hash": sealed_hash,
    })


def seal_snapshot_prefix(detail_key: str | None) -> bytes:
    """固定 envelope 的前缀仍由唯一 JCS 编码器处理转义和字段顺序。"""
    return canonical_json_bytes({
        "schema": "context-plan-seal:v1", "detail_ref": detail_key,
    })[:-1] + b',"snapshot":'


def runtime_seal_bytes_and_hash(
    snapshot: ContextAssemblySnapshot, detail_key: str | None, input_hash: str
) -> tuple[bytes, str]:
    """writer 编码一次完整 envelope，持久正文与摘要共用本次字节。"""
    _require_seal_input_hash(input_hash)
    sealed_bytes, sealed_hash = canonical_json_bytes_and_hash({
        "schema": "context-plan-seal:v1",
        "snapshot": snapshot.to_dict(),
        "detail_ref": detail_key,
    })
    return sealed_bytes, _runtime_seal_hash(sealed_hash, input_hash)


def persisted_runtime_seal_hash(
    snapshot_json: str, detail_key: str | None, input_hash: str,
) -> str:
    """cold 读取直接校验本次 SQL 的规范正文，不再投影和重编码大 snapshot。

    domain、plan/request 与 registry 仍由 reader 完整校验；持久文本的任何
    字节变化都必须与原 writer 的 seal 摘要重新比对，不能信任已存摘要。
    """
    _require_seal_input_hash(input_hash)
    sealed = sha256(seal_snapshot_prefix(detail_key))
    sealed.update(snapshot_json.encode("utf-8"))
    sealed.update(b"}")
    return _runtime_seal_hash("sha256:jcs:v1:" + sealed.hexdigest(), input_hash)
