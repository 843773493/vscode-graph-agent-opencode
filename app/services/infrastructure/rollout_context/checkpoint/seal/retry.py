"""仅从已注册来源和已提交 detail identity 比较重试，不分配或写入正文。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    draft_manifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    ContextPlanRegistration,
)
from app.services.mapping.itemized.selection import VerifiedRequestBody


def require_key(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"assembly-idempotency-conflict: {field} 必须非空")
    return value


def runtime_draft(registered: ContextPlanRegistration) -> ContextRequestPlan:
    if registered.registration_origin != "runtime" or registered.draft is None:
        raise ValueError(
            "plan-order-integrity: imported registration 不允许 runtime create/revise/seal"
        )
    return registered.draft


def seal_input_hash(
    draft: ContextRequestPlan,
    checkpoint_ns: str,
    assembly_options: Mapping[str, object],
    request_input_hash: str | None,
) -> str:
    return sha256_jcs(
        {
            "schema": "saver-context-seal-input:v1",
            "session_id": draft.session_id,
            "checkpoint_ns": checkpoint_ns,
            "draft_manifest": draft_manifest(draft),
            "assembly_options": {
                **assembly_options,
                "omitted_ref_ids": sorted(assembly_options["omitted_ref_ids"]),
                "loss": list(assembly_options["loss"]),
            },
            "request_input_hash": request_input_hash,
        }
    )


def require_registered_draft(
    owner, session_id: str, plan: ContextRequestPlan, checkpoint_ns: str
):
    owner._require_context_plan_owner(session_id, plan)
    if plan.plan_state != "unsealed" or plan.assembly_id is not None:
        raise ValueError("plan-order-integrity: seal 输入必须是已注册 unsealed plan")
    registered = owner.get_context_plan_registration(
        session_id, plan_id=plan.plan_id, checkpoint_ns=checkpoint_ns
    )
    if draft_manifest(plan) != draft_manifest(runtime_draft(registered)):
        raise ValueError(
            "assembly-idempotency-conflict: seal 输入与当前注册 draft 不一致"
        )
    return registered


def committed_retry(
    owner,
    registered,
    *,
    seal_idempotency_key: str,
    input_hash: str,
    checkpoint_ns: str,
    assembly_options: Mapping[str, object],
    request_only_content: Mapping[str, object] | None,
) -> ContextAssemblySnapshot | None:
    draft = runtime_draft(registered)
    if registered.plan_state == "unsealed":
        return None
    if (
        registered.seal_idempotency_key != seal_idempotency_key
        or registered.seal_input_hash != input_hash
    ):
        raise ValueError("assembly-idempotency-conflict: plan 已绑定另一个 seal key")
    committed = owner._storage.get_context_assembly(
        draft.session_id,
        assembly_id=registered.assembly_id,
        checkpoint_ns=checkpoint_ns,
    )
    # 只复用已经存在的 included detail mapping。omission 改变时由 composer
    # 严格拒绝多余/缺失 binding，不能为重试临时补造 locator 或读取当前 source。
    detail_refs = {
        entry.ref.ref_id: entry.detail_ref
        for entry in committed.selection
        if entry.included and entry.ref.ref_type == "request_only"
    }
    try:
        candidate = owner._composer_for(draft.session_id, checkpoint_ns).assembly(
            plan=draft,
            assembly_id=committed.assembly_id,
            session_id=draft.session_id,
            request_detail_refs=detail_refs,
            **assembly_options,
        )
    except (ValueError, TypeError) as error:
        raise ValueError(
            "assembly-idempotency-conflict: 重试 selection/preimage 不一致"
        ) from error
    if canonical_json_bytes(candidate.to_dict()) != canonical_json_bytes(
        committed.to_dict()
    ):
        raise ValueError("assembly-idempotency-conflict: 重试请求 preimage 不一致")
    included_body_keys = {
        key
        for entry in committed.selection
        if entry.included and entry.ref.ref_type == "request_only"
        for key in (entry.ref.ref_id, entry.contribution_id)
        if key is not None
    }
    explicit_included = {
        key: value
        for key, value in (request_only_content or {}).items()
        if key in included_body_keys
    }
    if explicit_included:
        # 调用者正文只能用于等值验证；从 committed detail 读取的值才是权威。
        persisted = owner._request_content_for_plan(
            draft.session_id, checkpoint_ns, committed.as_sealed_plan(), None
        )
        for key, value in explicit_included.items():
            body = persisted.get(key)
            if isinstance(body, VerifiedRequestBody):
                body = body.body
            if key not in persisted or canonical_json_bytes(
                value
            ) != canonical_json_bytes(body):
                raise ValueError(
                    "assembly-idempotency-conflict: 重试 request-only 正文不一致"
                )
    return committed
