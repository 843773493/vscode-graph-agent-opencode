"""ContextRequestPlan 的实时组装与 hash 计算。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import (
    ContextAssemblySnapshot,
    context_request_hash,
)
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    BaseDeltaRole,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    resolve_contribution_for_ref,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.assembly.manifest import (
    validate_sealed_selection,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_context.runtime.ledger import (
    RuntimeContextLedger,
)


class ContextPlanComposer:
    """只消费调用方传入的已提交 refs，不旁路扫描 storage/context reader。"""

    def __init__(self, ledger: RuntimeContextLedger | None = None) -> None:
        self.ledger = ledger or RuntimeContextLedger()

    def compose(
        self,
        *,
        session_id: str,
        plan_id: str,
        refs: Sequence[ContextRef],
        tool_snapshot: Sequence[Mapping[str, object]] = (),
        history_view_revision: int = 0,
        source_overlay_epoch: int = 0,
        compiler_version: str = "itemized-context-v1",
        active_view_id: str | None = None,
        selection_policy: str = "active_view",
    ) -> ContextRequestPlan:
        contribution_refs: list[ContextRef] = []
        for contribution in self.ledger.snapshot_contributions():
            # overlay base/delta contributions are manifest backing records for
            # an explicit overlay ref. They must not also become a second
            # request-only selection entry; the assembly selection carries the
            # contribution_id binding exactly once. 角色由 typed
            # contribution_kind 闭合集承载，metadata 中的 selection_only
            # 历史 flag 不再拥有解释权。
            if contribution.contribution_kind in {"overlay_base", "overlay_delta"}:
                continue
            if contribution.content_length is None:
                raise DetailUnavailableError(
                    f"context contribution 缺少 content_length: {contribution.contribution_id}"
                )
            contribution_refs.append(
                ContextRef.request_only_ref(
                    contribution.contribution_id,
                    session_id=session_id,
                    plan_id=plan_id,
                    source_revision=contribution.source_revision,
                    semantic_kind=SemanticKind.RUNTIME_NOTICE.value
                    if contribution.contribution_kind == "notice"
                    else SemanticKind.EXTENSION.value,
                    payload_kind=PayloadKind.STRUCTURED_CONTENT.value,
                    content_length=contribution.content_length,
                    content_hash_value=contribution.content_hash,
                    redacted_stable_digest=contribution.redacted_stable_digest,
                    source_ref=contribution.contribution_id,
                    protection=contribution.protection,
                    visibility=contribution.visibility,
                )
            )
        known = {(ref.ref_type, ref.ref_id) for ref in refs}
        normalized_refs = tuple(
            [ref for ref in contribution_refs if (ref.ref_type, ref.ref_id) not in known]
            + list(refs)
        )
        source_revision = sha256_jcs(
            {
                "tools": [dict(item) for item in tool_snapshot],
                "plan_id": plan_id,
            }
        )
        tool_set_refs = (
            (
                ToolSetRef.from_tool_snapshot(
                    session_id=session_id,
                    snapshot_id=f"tool-set:{source_revision}",
                    plan_id=plan_id,
                    tools=tool_snapshot,
                    source_revision=source_revision,
                ),
            )
            if tool_snapshot
            else ()
        )
        return ContextRequestPlan(
            session_id=session_id,
            plan_id=plan_id,
            refs=normalized_refs,
            contributions=self.ledger.snapshot_contributions(),
            tool_snapshot=tuple(dict(item) for item in tool_snapshot),
            history_view_revision=history_view_revision,
            source_overlay_epoch=source_overlay_epoch,
            compiler_version=compiler_version,
            active_view_id=active_view_id,
            selection_policy=selection_policy,
            tool_set_refs=tool_set_refs,
        )

    @staticmethod
    def request_hash(
        plan: ContextRequestPlan,
        provider: str,
        *,
        projector_id: str = "itemized-context-provider",
        projector_version: str = "v1",
        target_format: str = "unknown",
        wire_request: object | None = None,
    ) -> str:
        return context_request_hash(
            plan,
            provider,
            projector_id=projector_id,
            projector_version=projector_version,
            target_format=target_format,
            wire_request=wire_request,
        )

    def assembly(
        self,
        *,
        plan: ContextRequestPlan,
        assembly_id: str,
        session_id: str,
        turn_id: str,
        execution_id: str,
        provider_version: str,
        model_call_id: str | None = None,
        loss: Iterable[str] = (),
        projector_id: str = "itemized-context-provider",
        projector_version: str = "v1",
        target_format: str = "unknown",
        wire_request: object | None = None,
        omitted_ref_ids: Iterable[str] = (),
        request_detail_refs: Mapping[str, DetailRef] | None = None,
    ) -> ContextAssemblySnapshot:
        if plan.session_id != session_id:
            raise ValueError("source-mismatch: plan 与 assembly session 不一致")
        # source_ordinal 是 context_contributions 表的独立 registry 排序列，
        # 不是 contribution provenance metadata。它只在 ledger 重建时参与
        # 排序，不能复制进 sealed snapshot；否则 snapshot 与 registry 的
        # metadata_json 会发生虚假的 source-mismatch。
        registry_refs = tuple(plan.refs)
        detail_refs = dict(request_detail_refs) if request_detail_refs is not None else {}
        omitted_ids = frozenset(omitted_ref_ids)
        request_source_ids = {
            ref.ref_id for ref in registry_refs
            if ref.ref_type == "request_only"
            and ref.ref_id not in omitted_ids
            and ref.availability == "available"
        }
        for ref_id, detail_ref in detail_refs.items():
            if not isinstance(ref_id, str) or ref_id not in request_source_ids:
                raise ValueError(
                    "plan-order-integrity: final detail 只能绑定 included request-only ref"
                )
            if not isinstance(detail_ref, DetailRef):
                raise TypeError("source-mismatch: final detail 必须是 typed DetailRef")
            detail_ref.require_owner(session_id, assembly_id)
        known_ids = {ref.ref_id for ref in registry_refs}
        known_ids.update(ref.ref_id for ref in plan.tool_set_refs)
        unknown_omitted_ids = omitted_ids - known_ids
        if unknown_omitted_ids:
            raise ValueError(
                "plan-order-integrity: omitted ref 不在当前 plan registry: "
                + ",".join(sorted(unknown_omitted_ids))
            )

        def contribution_for_ref(
            ref: ContextRef,
        ) -> ContextContribution | None:
            # 所有 request-only/overlay ref 统一走 domain binding resolver；
            # 不允许 composer 与 detail binder 各自维护一套 alias 匹配规则。
            return resolve_contribution_for_ref(ref, plan.contributions)

        selected_contribution_ids = {
            contribution.contribution_id
            for ref in registry_refs
            if ref.availability == "available" and ref.ref_id not in omitted_ids
            for contribution in (contribution_for_ref(ref),)
            if contribution is not None
        }
        assembly_contributions = tuple(
            replace(
                contribution,
                metadata={
                    key: value
                    for key, value in contribution.metadata.items()
                    if key != "source_ordinal"
                },
            )
            for contribution in plan.contributions
            if contribution.contribution_id in selected_contribution_ids
        )
        contributions = tuple(
            ContextContribution(
                contribution_id=contribution.contribution_id,
                source_kind=contribution.source_kind,
                source_revision=contribution.source_revision,
                content_hash=contribution.content_hash,
                request_only=contribution.request_only,
                metadata=contribution.metadata,
                contribution_kind=contribution.contribution_kind,
                body=contribution.body,
                content_length=contribution.content_length,
                redacted_stable_digest=contribution.redacted_stable_digest,
                visibility=contribution.visibility,
                protection=contribution.protection,
                root_placement=contribution.root_placement,
                assembly_id=assembly_id,
                contribution_ordinal=ordinal,
            )
            for ordinal, contribution in enumerate(assembly_contributions)
        )
        contribution_by_id = {
            item.contribution_id: item for item in contributions
        }
        contribution_by_ref = {
            (ref.ref_type, ref.ref_id): contribution_for_ref(ref)
            for ref in registry_refs
        }
        selection_refs: list[ContextSelectionEntry] = []
        for ordinal, ref in enumerate(registry_refs):
            if ref.ref_type == "canonical_item":
                selection_kind = SelectionKind.CANONICAL_HISTORY.value
                base_delta_role = BaseDeltaRole.NONE.value
            elif ref.base_delta_role == BaseDeltaRole.BASE.value:
                selection_kind = SelectionKind.OVERLAY_BASE.value
                base_delta_role = BaseDeltaRole.BASE.value
            elif ref.base_delta_role == BaseDeltaRole.DELTA.value:
                selection_kind = SelectionKind.OVERLAY_DELTA.value
                base_delta_role = BaseDeltaRole.DELTA.value
            else:
                selection_kind = SelectionKind.REQUEST_ONLY.value
                base_delta_role = BaseDeltaRole.NONE.value
            contribution = (
                contribution_by_ref.get((ref.ref_type, ref.ref_id))
                if ref.ref_id not in omitted_ids
                else None
            )
            if contribution is not None:
                contribution = contribution_by_id.get(contribution.contribution_id)
            included = ref.availability == "available" and ref.ref_id not in omitted_ids
            selection_refs.append(
                ContextSelectionEntry(
                    assembly_id=assembly_id,
                    plan_ordinal=ordinal,
                    ref=ref,
                    selection_kind=selection_kind,
                    included=included,
                    omission_reason=(
                        None
                        if included
                        else (
                            "selection_omitted"
                            if ref.ref_id in omitted_ids
                            else f"source_{ref.availability}"
                        )
                    ),
                    loss=(
                        ()
                        if included
                        else (
                            "selection_omitted",
                        )
                        if ref.ref_id in omitted_ids
                        else (f"source_{ref.availability}",)
                    ),
                    visibility=ref.visibility,
                    protection=ref.protection,
                    availability=ref.availability,
                    source_revision=ref.source_revision,
                    content_length=ref.content_length,
                    content_hash=ref.content_hash,
                    redacted_stable_digest=ref.redacted_stable_digest,
                    detail_ref=(
                        detail_refs.get(ref.ref_id)
                        if ref.ref_type == "request_only" and included else None
                    ),
                    contribution_id=(
                        contribution.contribution_id
                        if contribution is not None
                        else None
                    ),
                    base_delta_role=base_delta_role,
                    source_overlay_epoch=ref.source_overlay_epoch,
                    overlay_from_revision=ref.overlay_from_revision,
                    overlay_to_revision=ref.overlay_to_revision,
                    overlay_diff_hash=ref.overlay_diff_hash,
                    contribution_ordinal=(
                        contribution.contribution_ordinal
                        if contribution is not None
                        else None
                    ),
                )
            )
        next_ordinal = len(selection_refs)
        bound_tool_refs: list[ToolSetRef] = []
        for tool_ref in plan.tool_set_refs:
            bound = ToolSetRef(
                session_id=tool_ref.session_id,
                ref_id=tool_ref.ref_id,
                plan_id=tool_ref.plan_id,
                source_revision=tool_ref.source_revision,
                tool_set_schema=tool_ref.tool_set_schema,
                tool_set_schema_version=tool_ref.tool_set_schema_version,
                tool_policy_version=tool_ref.tool_policy_version,
                content_length=tool_ref.content_length,
                content_hash=tool_ref.content_hash,
                redacted_stable_digest=tool_ref.redacted_stable_digest,
                protection=tool_ref.protection,
                availability=tool_ref.availability,
                tool_policy=tool_ref.tool_policy,
                tools=tool_ref.tools,
                assembly_id=assembly_id,
            )
            bound_tool_refs.append(bound)
            selection_refs.append(
                ContextSelectionEntry(
                    assembly_id=assembly_id,
                    plan_ordinal=next_ordinal,
                    ref=bound,
                    selection_kind=SelectionKind.TOOL_SET.value,
                    included=(
                        bound.availability == "available"
                        and bound.ref_id not in omitted_ids
                    ),
                    omission_reason=(
                        None
                        if bound.availability == "available"
                        and bound.ref_id not in omitted_ids
                        else (
                            "selection_omitted"
                            if bound.ref_id in omitted_ids
                            else f"source_{bound.availability}"
                        )
                    ),
                    loss=(
                        ()
                        if bound.availability == "available"
                        and bound.ref_id not in omitted_ids
                        else (
                            "selection_omitted",
                        )
                        if bound.ref_id in omitted_ids
                        else (f"source_{bound.availability}",)
                    ),
                    visibility="internal",
                    protection=bound.protection,
                    availability=bound.availability,
                    source_revision=bound.source_revision,
                    content_length=bound.content_length,
                    content_hash=bound.content_hash,
                    redacted_stable_digest=bound.redacted_stable_digest,
                    detail_ref=None,
                    contribution_id=None,
                )
            )
            next_ordinal += 1
        validate_sealed_selection(selection_refs)
        sealed_plan = plan.seal_for_assembly(
            assembly_id,
            selection=selection_refs,
        )
        return ContextAssemblySnapshot(
            assembly_id=assembly_id,
            session_id=session_id,
            turn_id=turn_id,
            execution_id=execution_id,
            plan_id=plan.plan_id,
            plan_hash=sealed_plan.plan_hash(),
            request_hash=self.request_hash(
                sealed_plan,
                provider_version,
                projector_id=projector_id,
                projector_version=projector_version,
                target_format=target_format,
                wire_request=wire_request,
            ),
            history_view_revision=sealed_plan.history_view_revision,
            source_overlay_epoch=sealed_plan.source_overlay_epoch,
            refs=sealed_plan.refs,
            contributions=contributions,
            tool_snapshot=sealed_plan.tool_snapshot,
            compiler_version=sealed_plan.compiler_version,
            provider_version=provider_version,
            active_view_id=sealed_plan.active_view_id,
            selection_policy=sealed_plan.selection_policy,
            model_call_id=model_call_id,
            loss=tuple(loss),
            sealed=True,
            tool_set_refs=tuple(bound_tool_refs),
            selection=tuple(selection_refs),
            projector_id=projector_id,
            projector_version=projector_version,
            target_format=target_format,
            request_hash_preimage=(
                normalize_wire_request(wire_request)
                if wire_request is not None
                else None
            ),
        )

__all__ = ["ContextPlanComposer"]
