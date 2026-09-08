"""plan registry 的无正文 manifest 和精确幂等 preimage。"""

from __future__ import annotations

import re
from dataclasses import fields, replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_plan import (
    ContextRequestPlan,
    resolve_contribution_for_ref,
)
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict
from app.services.infrastructure.rollout_context.assembly.plans.privacy import (
    validate_draft_privacy,
)


def json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def draft_manifest(plan: ContextRequestPlan) -> dict[str, object]:
    """registry 只保存 source manifest；正文仍通过 source/detail owner 恢复。"""
    if plan.plan_state != "unsealed" or plan.assembly_id is not None or plan.selection:
        raise ValueError("plan-order-integrity: plan registry 只接收 unsealed draft")
    if not plan.plan_creation_idempotency_key:
        raise ValueError("plan-idempotency-conflict: 创建 draft 必须有显式幂等键")
    validate_draft_privacy(plan)
    # frozen dataclass 不能阻止调用方修改嵌套 JSON；去正文前重新验证原始 body。
    for contribution in plan.contributions:
        replace(contribution)
    manifest = replace(
        plan,
        contributions=tuple(
            replace(contribution, body=None) for contribution in plan.contributions
        ),
    ).to_dict()
    # 即便调用方绕过构造器或修改嵌套对象，写入前也必须经过完整 domain parser。
    unsealed_context_plan_from_dict(manifest)
    return manifest


def creation_hash(plan: ContextRequestPlan) -> str:
    """创建输入不包含新分配的 plan owner；source/ref identity 与顺序仍被绑定。"""
    manifest = draft_manifest(plan)
    manifest.pop("plan_id")
    manifest.pop("plan_hash")
    for field, refs in (("refs", plan.refs), ("tool_set_refs", plan.tool_set_refs)):
        normalized_refs = [ref.to_dict() for ref in refs]
        for ref in normalized_refs:
            ref.pop("plan_id", None)
        manifest[field] = normalized_refs
    return sha256_jcs({"schema": "context-plan-creation:v1", "input": manifest})


def validate_sealed_plan(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> None:
    """只能将当前 draft 按给定 selection 封存，不能换入另一个 source registry。"""
    _validate_contribution_bindings(draft, snapshot)
    # registry 顺序不是请求顺序；持久 snapshot 仅保留 included 工具定义。
    # 先按 tagged identity 验证原始来源，再建立仅用于比较的无 omitted 正文视图。
    draft, snapshot = _tool_comparison_view(draft, snapshot)
    snapshot.validate_hashes()
    expected = draft.seal_for_assembly(
        snapshot.assembly_id, selection=snapshot.selection
    )
    actual = snapshot.as_sealed_plan()
    # creation key 属于独立 plan 生命周期；既有 snapshot 不持有它。
    actual = replace(
        actual, plan_creation_idempotency_key=draft.plan_creation_idempotency_key
    )
    expected = replace(
        expected,
        contributions=tuple(
            replace(item, body=None) for item in expected.contributions
        ),
    )
    actual = replace(
        actual,
        contributions=tuple(replace(item, body=None) for item in actual.contributions),
    )
    if canonical_json_bytes(expected.to_dict()) != canonical_json_bytes(
        actual.to_dict()
    ):
        raise ValueError(
            "plan-order-integrity: sealed snapshot 与当前 draft registry 不一致"
        )


def _validate_contribution_bindings(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> None:
    """omitted 可保留已存在的映射，但不能借跳过正文校验补造贡献身份。"""
    sources = {(ref.ref_type, ref.ref_id): ref for ref in draft.refs}
    for entry in snapshot.selection:
        if entry.contribution_id is None:
            continue
        source = sources.get((entry.ref.ref_type, entry.ref.ref_id))
        if source is None or source.ref_type != "request_only":
            raise ValueError("plan-order-integrity: contribution binding 缺少 request-only source")
        contribution = resolve_contribution_for_ref(source, draft.contributions)
        if contribution is None or contribution.contribution_id != entry.contribution_id:
            raise ValueError("plan-order-integrity: contribution binding 与 plan registry 不一致")
        for field in ("source_revision", "content_length", "content_hash", "redacted_stable_digest"):
            value = getattr(entry, field)
            if value is not None and value != getattr(contribution, field):
                raise ValueError("source-mismatch: contribution binding manifest 不一致")


def _tool_identity(ref: ToolSetRef, assembly_id: str) -> dict[str, object]:
    # 不经 to_dict 再删正文：omitted tools/policy 可能已经不可读取。
    return {
        **{
            field.name: getattr(ref, field.name)
            for field in fields(ToolSetRef)
            if field.name not in {"tools", "tool_policy", "assembly_id"}
        },
        "assembly_id": assembly_id,
    }


def _tool_comparison_view(
    draft: ContextRequestPlan, snapshot: ContextAssemblySnapshot
) -> tuple[ContextRequestPlan, ContextAssemblySnapshot]:
    sources = {(ref.ref_type, ref.ref_id): ref for ref in draft.tool_set_refs}
    for ref in (
        *snapshot.tool_set_refs,
        *(
            entry.ref
            for entry in snapshot.selection
            if isinstance(entry.ref, ToolSetRef)
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
    selection = tuple(
        replace(
            entry,
            ref=ToolSetRef(**_tool_identity(entry.ref, snapshot.assembly_id)),
        )
        if isinstance(entry.ref, ToolSetRef) and not entry.included
        else entry
        for entry in snapshot.selection
    )
    # 不改动持久 draft，也不从 snapshot 反向补造 source；included 正文仍由
    # 原始 draft 参与 domain manifest 校验与逐字段比较，selection 原序不变。
    return (
        replace(draft, tool_set_refs=tuple(sources[key] for key in sorted(included))),
        replace(
            snapshot,
            tool_set_refs=tuple(
                sorted(
                    (
                        ref
                        for ref in snapshot.tool_set_refs
                        if (ref.ref_type, ref.ref_id) in included
                    ),
                    key=lambda ref: (ref.ref_type, ref.ref_id),
                )
            ),
            selection=selection,
        ),
    )


def sealed_hash(snapshot: ContextAssemblySnapshot, detail_key: str | None) -> str:
    """绑定整份 sealed semantic identity，不能仅用忽略 provider 的 plan_hash 去重。"""
    return sha256_jcs(
        {
            "schema": "context-plan-seal:v1",
            "snapshot": snapshot.to_dict(),
            "detail_ref": detail_key,
        }
    )


def runtime_seal_hash(
    snapshot: ContextAssemblySnapshot, detail_key: str | None, input_hash: str
) -> str:
    """将调用前输入摘要与最终封存身份一起绑定，拒绝独立篡改重试依据。"""
    if not isinstance(input_hash, str) or re.fullmatch(r"sha256:jcs:v1:[0-9a-f]{64}", input_hash) is None:
        raise ValueError("assembly-idempotency-conflict: seal_input_hash 必须是规范 JCS SHA-256 摘要")
    return sha256_jcs({
        "schema": "context-plan-runtime-seal:v1",
        "seal_input_hash": input_hash,
        "sealed_hash": sealed_hash(snapshot, detail_key),
    })
