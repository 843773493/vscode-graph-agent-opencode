"""ContextRequestPlan 哈希范围的共享投影。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.itemized.refs import ContextRef

if TYPE_CHECKING:
    from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan


def hash_scope_for_plan(
    plan: ContextRequestPlan,
) -> tuple[
    tuple[object, ...],
    tuple[object, ...],
    tuple[ContextContribution, ...],
]:
    """按 plan 状态返回哈希范围内的 refs、工具 refs 和 contributions。"""
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
    return refs_for_hash, tool_set_refs_for_hash, contributions_for_hash
