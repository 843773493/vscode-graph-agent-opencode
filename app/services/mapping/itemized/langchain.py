"""itemized v2 的无 I/O context projection owner。

本模块只消费 Saver 已提交的 ``ContextRequestPlan``、canonical item 和
request-only body。它不打开 rollout storage、不重新排序 registry，也不把
ToolSetRef 伪造成 message；provider 工具 wire 由 infrastructure bridge 负责。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from langchain_core.messages import BaseMessage, SystemMessage

from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import (
    ToolSetRef,
    selection_ref_identity,
)
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.canonical_langchain import (
    project_canonical_items as _project_canonical_items,
)
from app.services.mapping.itemized.prefix_frames import project_sealed_prefix
from app.services.mapping.itemized.selection import (
    resolve_selected_item,
    resolve_selected_request_body,
    validate_projection_selection,
)


def project_context_plan(
    plan: ContextRequestPlan,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
    request_only_content: Mapping[str, object] | None = None,
    include_request_only: bool = True,
    seal_binding: ContextSealBinding | None = None,
) -> list[BaseMessage]:
    """只按 Saver seal 的 selection 生成消息，缺失或 mismatch 立即失败。"""
    if plan.plan_state != "sealed" or plan.assembly_id is None:
        raise ValueError("context plan 未 sealed，不能进入 projector")
    if seal_binding is not None and include_request_only:
        return project_sealed_prefix(
            plan, seal_binding, items, request_only_content or {},
        )
    ref_registry = {selection_ref_identity(ref): ref for ref in plan.refs}
    contribution_registry = {
        contribution.contribution_id: contribution
        for contribution in plan.contributions
    }
    bodies = request_only_content or {}
    messages: list[BaseMessage] = []
    canonical_run: list[CanonicalItemRecord] = []
    system_run: list[object] = []
    system_metadata: list[dict[str, object]] = []

    def flush_system() -> None:
        if not system_run:
            return
        messages.append(
            SystemMessage(
                content=list(system_run),
                id="request-only-system-prompt",
                response_metadata={
                    "request_only": True,
                    "selection_count": len(system_metadata),
                    "selection": tuple(system_metadata),
                },
            )
        )
        system_run.clear()
        system_metadata.clear()

    def flush_canonical() -> None:
        if canonical_run:
            messages.extend(
                _project_canonical_items(
                    tuple(canonical_run),
                    include_runtime_notices=include_runtime_notices,
                    include_summaries=include_summaries,
                    capability_losses=capability_losses,
                    preserve_order=True,
                )
            )
            canonical_run.clear()

    seen: set[tuple[str, ...]] = set()
    canonical_thread: str | None = None
    for entry in validate_projection_selection(plan.selection):
        ref = entry.ref
        identity = selection_ref_identity(ref)
        if identity in seen:
            raise ValueError(f"plan-order-integrity: selection 重复: {identity}")
        seen.add(identity)
        if entry.included and capability_losses is not None:
            capability_losses.extend(entry.loss)
        if not entry.included:
            flush_system()
            flush_canonical()
            if capability_losses is not None:
                capability_losses.extend(
                    entry.loss or (entry.omission_reason or "omitted",)
                )
            continue
        if isinstance(ref, ToolSetRef):
            flush_system()
            flush_canonical()
            if entry.availability != "available" or ref.availability != "available":
                raise ValueError(
                    "detail-unavailable: included ToolSetRef 不可用于 projection: "
                    f"{ref.ref_id}"
                )
            if ref.content_hash is None:
                raise ValueError(
                    "detail-unavailable: included ToolSetRef 没有可发送的 manifest: "
                    f"{ref.ref_id}"
                )
            continue
        if ref_registry.get(identity) != ref:
            raise ValueError(f"plan-order-integrity: selection ref 缺失: {ref.ref_id}")
        if ref.ref_type == "canonical_item":
            flush_system()
            item = resolve_selected_item(entry, items)
            if canonical_thread != ref.thread_id:
                flush_canonical()
            canonical_thread = ref.thread_id
            canonical_run.append(item)
            continue
        if not include_request_only:
            # 默认 Web/history projection 不 materialize request-only 正文。
            # selection 仍已由同一个 sealed plan 校验并确定顺序；这里不能
            # 回退当前 middleware registry，也不能把缺正文伪造成空 system。
            flush_canonical()
            continue
        flush_canonical()
        body = resolve_selected_request_body(plan, entry, bodies)
        contribution = contribution_registry.get(entry.contribution_id)
        if entry.contribution_id is not None and contribution is None:
            raise ValueError(
                "plan-order-integrity: request-only contribution manifest 缺失: "
                f"{entry.contribution_id}"
            )
        system_run.extend(body if isinstance(body, list) else [body])
        entry_metadata = {
            "context_ref_id": ref.ref_id,
            "ref_type": ref.ref_type,
            "session_id": ref.session_id,
            "plan_id": ref.plan_id,
            "source_ref": ref.source_ref.to_dict()
            if isinstance(ref.source_ref, DetailRef)
            else ref.source_ref,
            "source_revision": ref.source_revision,
            "content_hash": ref.content_hash,
            "content_length": ref.content_length,
            "detail_ref": entry.detail_ref.to_dict() if entry.detail_ref else None,
            "request_only": True,
            "selection_kind": entry.selection_kind,
            "plan_ordinal": entry.plan_ordinal,
            "assembly_id": entry.assembly_id,
            "contribution_ordinal": entry.contribution_ordinal,
            "loss": entry.loss,
            "visibility": entry.visibility,
            "protection": entry.protection,
            "availability": entry.availability,
            "base_delta_role": entry.base_delta_role,
            "source_overlay_epoch": entry.source_overlay_epoch,
        }
        if contribution is not None:
            entry_metadata.update(
                {
                    "context_contribution_id": contribution.contribution_id,
                    "source_kind": contribution.source_kind,
                    "content_hash": contribution.content_hash,
                }
            )
        system_metadata.append(entry_metadata)
    flush_canonical()
    flush_system()
    return messages


def contribution_metadata(
    contributions: Iterable[ContextContribution],
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "contribution_id": item.contribution_id,
            "source_kind": item.source_kind,
            "source_revision": item.source_revision,
            "content_hash": item.content_hash,
            "request_only": item.request_only,
        }
        for item in contributions
    )


__all__ = [
    "contribution_metadata",
    "project_context_plan",
]
