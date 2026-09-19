"""ContextRequestPlan 的规范哈希投影，不负责生命周期和 I/O。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.serialization import _hash_safe_value

if TYPE_CHECKING:
    from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan


def context_plan_hash(plan: ContextRequestPlan) -> str:
    # sealed plan 的 selection 是唯一的 hash scope。registry 允许保留
    # 未选中的候选来源，但它们不得改变已经提交的 plan；否则同一
    # assembly 在 middleware 动态追加无关 contribution 时会发生假
    # mismatch。unsealed plan 尚无 selection，因此保留完整 registry，
    # 供创建阶段的幂等检查使用。
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
    refs = [
        {
            "session_id": ref.session_id,
            "plan_id": ref.plan_id,
            "ref_id": ref.ref_id,
            "ref_type": ref.ref_type,
            "semantic_kind": ref.semantic_kind,
            "payload_kind": ref.payload_kind,
            "status": ref.status,
            "item_sequence": ref.item_sequence,
            "content_hash": ref.content_hash,
            "redacted_stable_digest": ref.redacted_stable_digest,
                    "content_length": ref.content_length,
                    "source_revision": ref.source_revision,
                    "base_delta_role": ref.base_delta_role,
                    "source_overlay_epoch": ref.source_overlay_epoch,
                    "overlay_from_revision": ref.overlay_from_revision,
                    "overlay_to_revision": ref.overlay_to_revision,
                    "overlay_diff_hash": ref.overlay_diff_hash,
                }
        for ref in sorted(
            refs_for_hash, key=lambda value: (value.ref_type, value.ref_id)
        )
    ]
    selection = [
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
            "ref": (
                {
                    "session_id": entry.ref.session_id,
                    "plan_id": entry.ref.plan_id,
                    "ref_type": entry.ref.ref_type,
                    "ref_id": entry.ref.ref_id,
                    "source_revision": entry.ref.source_revision,
                    "content_length": entry.ref.content_length,
                    "content_hash": entry.ref.content_hash,
                    "redacted_stable_digest": entry.ref.redacted_stable_digest,
                    "overlay_from_revision": (
                        entry.ref.overlay_from_revision
                        if isinstance(entry.ref, ContextRef)
                        else None
                    ),
                    "overlay_to_revision": (
                        entry.ref.overlay_to_revision
                        if isinstance(entry.ref, ContextRef)
                        else None
                    ),
                    "overlay_diff_hash": (
                        entry.ref.overlay_diff_hash
                        if isinstance(entry.ref, ContextRef)
                        else None
                    ),
                }
            ),
        }
        for entry in plan.selection
    ]
    tool_set_refs = [
        {
            "session_id": ref.session_id,
            "plan_id": ref.plan_id,
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
        for ref in sorted(tool_set_refs_for_hash, key=lambda value: value.ref_id)
    ]
    def contribution_order_key(item: ContextContribution) -> tuple[int, str]:
        # source_ordinal 只来自 itemized registry 分配的 typed 字段；
        # metadata 中的同名历史键不再参与 hash 排序。
        source_ordinal = item.source_ordinal
        ordinal = (
            item.contribution_ordinal
            if item.contribution_ordinal is not None
            else source_ordinal
        )
        if ordinal is None:
            raise ItemSchemaError(
                "ContextRequestPlan hash scope 缺少 contribution ordinal: "
                f"{item.contribution_id}"
            )
        return (ordinal, item.contribution_id)

    contribution_rows = []
    for item in sorted(contributions_for_hash, key=contribution_order_key):
        source_ordinal = item.source_ordinal
        if item.contribution_ordinal is None and source_ordinal is None:
            raise ItemSchemaError(
                "ContextRequestPlan hash scope 缺少 contribution ordinal: "
                f"{item.contribution_id}"
            )
        contribution_rows.append(
            {
                "source_ordinal": source_ordinal,
                "contribution_ordinal": item.contribution_ordinal,
                "contribution_id": item.contribution_id,
                "source_kind": item.source_kind,
                "contribution_kind": item.contribution_kind,
                "source_revision": item.source_revision,
                "content_length": item.content_length,
                "content_hash": item.content_hash,
                "redacted_stable_digest": item.redacted_stable_digest,
                "request_only": item.request_only,
                "visibility": item.visibility,
                "protection": item.protection,
                # root 资格决定投影 wire role，必须进入 sealed plan hash，
                # 防止 seal 后改声明改变投影而不改 plan_hash。
                "root_placement": item.root_placement,
            }
        )
    return sha256_jcs(
        {
            "schema": "context-plan-hash:v2",
            "format_version": plan.format_version,
            "active_view_id": plan.active_view_id,
            "history_view_revision": plan.history_view_revision,
            "source_overlay_epoch": plan.source_overlay_epoch,
            "selection_policy": plan.selection_policy,
            "refs": refs,
            "selection": selection,
            "tool_set_refs": tool_set_refs,
            "contributions": contribution_rows,
            "compiler_version": plan.compiler_version,
        }
    )
