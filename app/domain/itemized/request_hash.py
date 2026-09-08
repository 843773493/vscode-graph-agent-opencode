"""sealed context request hash 的纯领域实现。"""

from __future__ import annotations

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serialization import (
    _contribution_order_key,
    _hash_safe_value,
    normalize_wire_request,
)


def context_request_hash(
    plan: ContextRequestPlan,
    provider: str,
    *,
    projector_id: str = "itemized-context-provider",
    projector_version: str = "v1",
    target_format: str = "unknown",
    wire_request: object | None = None,
) -> str:
    """为已构造的 plan 计算 provider/projector 相关 request fingerprint。"""
    for name, value in (
        ("provider", provider),
        ("projector_id", projector_id),
        ("projector_version", projector_version),
        ("target_format", target_format),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"request hash {name} 不能为空")
    if plan.plan_state == "sealed":
        selected_keys = {
            (entry.ref.ref_type, entry.ref.ref_id) for entry in plan.selection
        }
        refs_for_hash = tuple(
            ref
            for ref in plan.refs
            if (ref.ref_type, ref.ref_id) in selected_keys
        )
        tool_set_refs_for_hash = tuple(
            ref
            for ref in plan.tool_set_refs
            if any(entry.included and entry.ref == ref for entry in plan.selection)
        )
        contribution_ids = {
            entry.contribution_id
            for entry in plan.selection
            if isinstance(entry.ref, ContextRef)
            and entry.ref.ref_type == "request_only"
            and entry.contribution_id is not None
        }
        contributions_for_hash = tuple(
            contribution
            for contribution in plan.contributions
            if contribution.contribution_id in contribution_ids
        )
    else:
        refs_for_hash = plan.refs
        tool_set_refs_for_hash = plan.tool_set_refs
        contributions_for_hash = plan.contributions

    normalized_wire_request = wire_request
    if normalized_wire_request is None:
        normalized_wire_request = {
            "target_format": target_format,
            "selection": [
                {
                    "plan_ordinal": entry.plan_ordinal,
                    "selection_kind": entry.selection_kind,
                    "included": entry.included,
                    "ref_type": entry.ref.ref_type,
                    "ref_id": entry.ref.ref_id,
                    "source_revision": entry.source_revision,
                    "content_length": entry.content_length,
                    "content_hash": entry.content_hash,
                    "redacted_stable_digest": entry.redacted_stable_digest,
                    "base_delta_role": entry.base_delta_role,
                    "source_overlay_epoch": entry.source_overlay_epoch,
                    "overlay_from_revision": entry.overlay_from_revision,
                    "overlay_to_revision": entry.overlay_to_revision,
                    "overlay_diff_hash": entry.overlay_diff_hash,
                    "contribution_ordinal": entry.contribution_ordinal,
                    "detail_ref": entry.detail_ref.to_dict() if entry.detail_ref is not None else None,
                    "contribution_id": entry.contribution_id,
                    "omission_reason": entry.omission_reason,
                    "loss": list(entry.loss),
                }
                for entry in plan.selection
            ],
        }
    normalized_selection = [
        {
            "plan_ordinal": entry.plan_ordinal,
            "selection_kind": entry.selection_kind,
            "included": entry.included,
            "omission_reason": entry.omission_reason,
            "loss": list(entry.loss),
            "visibility": entry.visibility,
            "protection": entry.protection,
            "availability": entry.availability,
            "source_revision": entry.source_revision,
            "content_length": entry.content_length,
            "content_hash": entry.content_hash,
            "redacted_stable_digest": entry.redacted_stable_digest,
            "base_delta_role": entry.base_delta_role,
            "source_overlay_epoch": entry.source_overlay_epoch,
            "overlay_from_revision": entry.overlay_from_revision,
            "overlay_to_revision": entry.overlay_to_revision,
            "overlay_diff_hash": entry.overlay_diff_hash,
            "contribution_ordinal": entry.contribution_ordinal,
            "detail_ref": entry.detail_ref.to_dict() if entry.detail_ref is not None else None,
            "contribution_id": entry.contribution_id,
            "ref": {
                "ref_type": entry.ref.ref_type,
                "ref_id": entry.ref.ref_id,
                "source_revision": entry.ref.source_revision,
                "content_length": entry.ref.content_length,
                "content_hash": entry.ref.content_hash,
                "redacted_stable_digest": entry.ref.redacted_stable_digest,
            },
        }
        for entry in plan.selection
    ]
    return sha256_jcs(
        {
            "plan_hash": plan.plan_hash(),
            "provider": provider,
            "projector_id": projector_id,
            "projector_version": projector_version,
            "target_format": target_format,
            "refs": [
                {
                    "ref_id": ref.ref_id,
                    "ref_type": ref.ref_type,
                    "semantic_kind": ref.semantic_kind,
                    "payload_kind": ref.payload_kind,
                    "status": ref.status,
                    "source_revision": ref.source_revision,
                    "content_length": ref.content_length,
                    "content_hash": ref.content_hash,
                    "redacted_stable_digest": ref.redacted_stable_digest,
                    "overlay_from_revision": ref.overlay_from_revision,
                    "overlay_to_revision": ref.overlay_to_revision,
                    "overlay_diff_hash": ref.overlay_diff_hash,
                    "base_delta_role": ref.base_delta_role,
                    "source_overlay_epoch": ref.source_overlay_epoch,
                }
                for ref in sorted(
                    refs_for_hash,
                    key=lambda value: (value.ref_type, value.ref_id),
                )
            ],
            "contributions": [
                {
                    "contribution_id": contribution.contribution_id,
                    "source_revision": contribution.source_revision,
                    "content_length": contribution.content_length,
                    "content_hash": contribution.content_hash,
                    "redacted_stable_digest": contribution.redacted_stable_digest,
                    "contribution_ordinal": contribution.contribution_ordinal,
                }
                for contribution in sorted(
                    contributions_for_hash,
                    key=_contribution_order_key,
                )
            ],
            "tool_set_refs": [
                {
                    "tool_set_snapshot_id": ref.ref_id,
                    "source_revision": ref.source_revision,
                    "content_length": ref.content_length,
                    "content_hash": ref.content_hash,
                    "redacted_stable_digest": ref.redacted_stable_digest,
                    "tool_set_schema": ref.tool_set_schema,
                    "tool_set_schema_version": ref.tool_set_schema_version,
                    "tool_policy_version": ref.tool_policy_version,
                    "tool_policy": _hash_safe_value(ref.tool_policy),
                }
                for ref in sorted(
                    tool_set_refs_for_hash,
                    key=lambda value: value.ref_id,
                )
            ],
            "selection": normalized_selection,
            "tool_snapshot": (
                _hash_safe_value(list(plan.tool_snapshot))
                if plan.plan_state != "sealed"
                else []
            ),
            "wire_request": normalize_wire_request(normalized_wire_request),
        }
    )


__all__ = ["context_request_hash"]
