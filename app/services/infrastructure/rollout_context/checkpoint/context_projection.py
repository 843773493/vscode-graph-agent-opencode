"""Saver context projection 的 source/detail 校验 owner。"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import BaseMessage

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import (
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import (
    ContextRequestPlan,
    resolve_contribution_for_ref,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailRecord,
    DetailUnavailableError,
    detail_record_from_mapping,
)
from app.services.mapping.itemized.selection import VerifiedRequestBody


class ContextProjectionOwnerMixin:
    """负责已 seal plan 的正文来源校验和 assembly detail locator 绑定。"""

    @staticmethod
    def _require_context_plan_owner(session_id: str, plan: ContextRequestPlan) -> None:
        """先验证调用方 owner；即使没有 refs，也不能初始化或读取另一 session。"""
        if plan.session_id != session_id:
            raise ValueError(
                "source-mismatch: context plan session owner 不一致: "
                f"plan={plan.session_id}, requested={session_id}"
            )

    def _validate_snapshot_sources(
        self, snapshot: ContextAssemblySnapshot, checkpoint_ns: str
    ) -> None:
        """在提交 assembly 前校验实际 canonical/source 正文，omitted 不解引用。"""
        from app.services.mapping.itemized.selection import resolve_selected_item

        entries = tuple(
            entry
            for entry in snapshot.selection
            if entry.included and entry.ref.ref_type == "canonical_item"
        )
        items = self._storage.read_items(
            snapshot.session_id,
            checkpoint_ns=checkpoint_ns,
            item_ids=tuple(entry.ref.ref_id for entry in entries),
        )
        by_id = {item.item_id: item for item in items}
        for entry in entries:
            resolve_selected_item(entry, by_id)
        self._request_content_for_plan(
            snapshot.session_id,
            checkpoint_ns,
            snapshot.as_sealed_plan(),
            None,
        )

    def project_context_plan_to_native(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """从同一已提交 selection 直接编码 Responses input，不经过 LangChain。"""
        self._require_context_plan_owner(session_id, plan)
        from app.services.infrastructure.rollout_context.provider.native_request import (
            project_native_request,
        )

        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        committed = self._committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
        )
        item_ids = tuple(
            entry.ref.ref_id
            for entry in committed.selection
            if entry.included and entry.ref.ref_type == "canonical_item"
        )
        return project_native_request(
            committed,
            self._storage.read_items(
                session_id, checkpoint_ns=checkpoint_ns, item_ids=item_ids
            ),
            self._request_content_for_plan(session_id, checkpoint_ns, committed, None),
        )

    def project_context_plan_to_history_with_diagnostics(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[list[BaseMessage], tuple[str, ...]]:
        """history 保留 selection loss，同时避免读取 request-only 正文。"""
        self._require_context_plan_owner(session_id, plan)
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        messages, _, losses = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_request_only=False,
            collect_capability_losses=True,
        )
        return messages, losses

    def _request_content_for_plan(
        self,
        session_id: str,
        checkpoint_ns: str,
        plan: ContextRequestPlan,
        explicit_content: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """只从 assembly-bound detail 恢复正文，并校验显式输入的一致性。"""
        self._require_context_plan_owner(session_id, plan)
        if plan.plan_state != "sealed" or plan.assembly_id is None:
            raise ValueError("context plan 未 sealed，不能解析 request-only 正文")
        # sealed identity 不再读取可能属于其它 plan/revision 的实时 source cache。
        # 显式正文仅用于相等性校验，不能替代下方必须存在的最终 detail。
        result: dict[str, object] = {}
        for contribution in plan.contributions:
            if contribution.body is not None:
                result.setdefault(contribution.contribution_id, contribution.body)
        if explicit_content is not None:
            result.update(explicit_content)
        verified_digests: dict[str, str] = {}
        selected_request_entries = {
            entry.ref.ref_id: entry
            for entry in plan.selection
            if entry.included
            and isinstance(entry.ref, ContextRef)
            and entry.ref.ref_type == "request_only"
        }
        selected_contribution_ids = {
            entry.contribution_id
            for entry in selected_request_entries.values()
            if entry.contribution_id is not None
        }
        # excluded/loss entry 只需保留 omission/loss，不应因为正文不可用而
        # 阻塞整个 plan；真正进入 wire 的 entry 才必须完成 source 校验。
        refs = {
            ref.ref_id: ref
            for ref in plan.refs
            if ref.ref_type == "request_only" and ref.ref_id in selected_request_entries
        }
        for ref_id, ref in refs.items():
            selection_entry = selected_request_entries[ref_id]
            body_key = selection_entry.contribution_id or ref_id
            if ref.availability != "available":
                raise DetailUnavailableError(
                    f"detail-unavailable: request-only source unavailable: {ref_id}"
                )
            detail_ref = selection_entry.detail_ref
            if not isinstance(detail_ref, DetailRef):
                raise DetailUnavailableError(
                    f"detail-unavailable: sealed request-only 缺少 typed detail: {ref_id}"
                )
            detail_ref.require_owner(session_id, plan.assembly_id)
            detail_record = self._request_source_record(
                session_id, checkpoint_ns, detail_ref, ref
            )
            detail_envelope = self._detail_store.read(
                session_id=session_id,
                record=detail_record,
                include_sensitive=ref.protection == "protected",
            )
            detail_body = detail_envelope.get("detail")
            if (
                detail_body is None
                or detail_envelope.get("protection") != ref.protection
                or ref.protection == "redacted"
            ):
                raise DetailUnavailableError(
                    f"detail-unavailable: detail has no permitted body: {ref_id}"
                )
            # 最终正文只从 sealed selection 的 detail 读取；source_ref 可以
            # 指向旧 assembly，但绝不能覆盖本 assembly 的最终 binding。
            if any(
                key in result and result[key] != detail_body
                for key in {ref_id, body_key}
            ):
                raise ValueError(f"source-mismatch: request-only detail body: {ref_id}")
            result[ref_id] = detail_body
            result[body_key] = detail_body
            if ref.redacted_stable_digest is not None:
                if (
                    ref.protection != "protected"
                    or detail_record.redacted_stable_digest
                    != ref.redacted_stable_digest
                    or detail_envelope.get("redacted_stable_digest")
                    != ref.redacted_stable_digest
                ):
                    raise ValueError(
                        f"source-mismatch: protected detail digest: {ref_id}"
                    )
                verified_digests[ref_id] = ref.redacted_stable_digest
                verified_digests[body_key] = ref.redacted_stable_digest
            body = result[ref_id]
            body_length = payload_content_length(
                ref.payload_kind or "structured_content",
                body,
            )
            contribution = next(
                (
                    contribution
                    for contribution in plan.contributions
                    if contribution.contribution_id == selection_entry.contribution_id
                ),
                None,
            )
            if (
                contribution is not None
                and contribution.source_revision != ref.source_revision
            ):
                raise ValueError(
                    f"source-mismatch: request-only contribution source_revision: {ref_id}"
                )
            body_hash = (
                contribution_content_hash(contribution.contribution_kind, body)
                if contribution is not None
                else sha256_jcs(body)
            )
            expected_token = ref.content_hash or ref.redacted_stable_digest
            actual_token = (
                body_hash
                if ref.content_hash is not None
                else verified_digests.get(ref_id)
            )
            if body_length != ref.content_length or actual_token != expected_token:
                raise ValueError(f"source-mismatch: request-only ref: {ref_id}")
        for contribution in plan.contributions:
            if not contribution.request_only:
                continue
            contribution_id = contribution.contribution_id
            if contribution_id not in selected_contribution_ids:
                continue
            if contribution_id not in result:
                raise DetailUnavailableError(
                    f"detail-unavailable: contribution source missing: {contribution_id}"
                )
            body = result[contribution_id]
            expected_token = (
                contribution.content_hash or contribution.redacted_stable_digest
            )
            if (
                contribution.content_length
                != payload_content_length("structured_content", body)
                or (
                    contribution_content_hash(contribution.contribution_kind, body)
                    if contribution.content_hash is not None
                    else verified_digests.get(contribution_id)
                )
                != expected_token
            ):
                raise ValueError(
                    f"source-mismatch: context contribution: {contribution_id}"
                )
        return {
            key: VerifiedRequestBody(body, verified_digests[key])
            if key in verified_digests
            else body
            for key, body in result.items()
        }

    def _request_source_record(
        self,
        session_id: str,
        checkpoint_ns: str,
        detail_ref: DetailRef,
        ref: ContextRef,
    ) -> DetailRecord:
        """request source 的 typed owner 与用途必须在读取正文前一致。"""
        detail_ref.require_owner(session_id)
        record = detail_record_from_mapping(
            self._storage.get_context_plan_detail(
                session_id, detail_ref=detail_ref, checkpoint_ns=checkpoint_ns
            )
        )
        if (
            record.detail_ref != detail_ref
            or record.detail_kind != "request_source"
            or record.retention_class != "request_replay"
            or record.visibility != ref.visibility
            or record.source_revision != ref.source_revision
            or record.length != ref.content_length
            or record.protection != ref.protection
        ):
            raise ValueError(
                f"source-mismatch: request source detail manifest: {ref.ref_id}"
            )
        if record.availability != "available":
            raise DetailUnavailableError(
                f"detail-unavailable: detail manifest unavailable: {ref.ref_id}"
            )
        return record

    def _recover_request_source(
        self,
        session_id: str,
        checkpoint_ns: str,
        ref: ContextRef,
        contribution_id: str | None,
    ) -> object:
        """新 assembly 复用旧 sealed source 的详情，绝不读取当前源文件。"""
        if isinstance(ref.source_ref, DetailRef):
            ref.source_ref.require_owner(session_id)
        for snapshot in self._storage.list_context_assemblies(
            session_id,
            checkpoint_ns=checkpoint_ns,
        ):
            for entry in snapshot.selection:
                previous = entry.ref
                if not entry.included or previous.ref_type != "request_only":
                    continue
                if isinstance(ref.source_ref, DetailRef):
                    matches = (
                        entry.detail_ref == ref.source_ref
                        and entry.contribution_id == contribution_id
                    )
                elif contribution_id is not None:
                    matches = entry.contribution_id == contribution_id
                else:
                    matches = entry.contribution_id is None and (
                        previous.source_ref == ref.source_ref
                        if ref.source_ref is not None
                        else previous.ref_id == ref.ref_id
                        and previous.plan_id == ref.plan_id
                    )
                if not matches or any(
                    getattr(previous, field) != getattr(ref, field)
                    for field in (
                        "source_revision",
                        "content_length",
                        "content_hash",
                        "redacted_stable_digest",
                        "protection",
                        "visibility",
                        "base_delta_role",
                        "source_overlay_epoch",
                        "overlay_from_revision",
                        "overlay_to_revision",
                        "overlay_diff_hash",
                    )
                ):
                    continue
                if not isinstance(entry.detail_ref, DetailRef):
                    raise DetailUnavailableError(
                        f"detail-unavailable: recovered source 缺少 typed detail: {ref.ref_id}"
                    )
                entry.detail_ref.require_owner(session_id, snapshot.assembly_id)
                record = self._request_source_record(
                    session_id, checkpoint_ns, entry.detail_ref, ref
                )
                envelope = self._detail_store.read(
                    session_id=session_id,
                    record=record,
                    include_sensitive=ref.protection == "protected",
                )
                return envelope["detail"]
        raise DetailUnavailableError(
            f"detail-unavailable: no committed request-only source: {ref.ref_id}"
        )

    def _bind_request_detail_refs(
        self,
        session_id: str,
        checkpoint_ns: str,
        plan: ContextRequestPlan,
        *,
        assembly_id: str,
        omitted_ref_ids: frozenset[str] = frozenset(),
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[ContextRequestPlan, tuple[DetailRecord, ...], dict[str, DetailRef]]:
        """为本次 sealed assembly 建立 target-local request detail binding。

        `ContextRef.ref_id` 是 source/plan identity，不是 contribution identity，
        也不是 detail path。每个 included request-only entry 都必须拥有同一
        assembly 下的 detail_ref；contribution-backed entry 另外通过显式
        `contribution_id` 关联 manifest。正文来自当前已登记的实时 body 或
        同一 source identity 的旧 sealed detail，不读取当前源文件，
        也不把正文写入 canonical item。
        """
        self._require_context_plan_owner(session_id, plan)
        if plan.plan_state != "unsealed" or plan.assembly_id is not None:
            raise ValueError(
                "plan-order-integrity: 只能为 unsealed plan 分配 detail binding"
            )
        records: list[DetailRecord] = []
        pending: list[tuple[ContextRef, object]] = []
        for ref in plan.refs:
            if ref.ref_type != "request_only":
                continue
            if ref.ref_id in omitted_ref_ids:
                # omitted entry 只保留 source identity，由 assembly composer
                # 创建 tagged selection；不能为了未发送的 source 生成 detail。
                continue
            if ref.availability != "available":
                # optional omission 由 sealed selection 表达，不分配 detail
                # binding，也不触发正文读取或生成替代详情。
                continue
            if ref.protection == "redacted" or (
                ref.protection == "protected"
                and not self._detail_store.supports_protected_details
            ):
                raise DetailUnavailableError(
                    "detail-unavailable: non-public request-only source cannot be"
                    f" dispatched without a protected detail backend: {ref.ref_id}"
                )
            contribution = resolve_contribution_for_ref(
                ref,
                plan.contributions,
            )
            content_key = (
                contribution.contribution_id if contribution is not None else ref.ref_id
            )
            body = (
                contribution.body
                if contribution is not None and contribution.body is not None
                else self._plan_request_content.get(
                    (session_id, checkpoint_ns, plan.plan_id, content_key),
                    self._request_only_content.get((session_id, checkpoint_ns, content_key)),
                )
            )
            if request_only_content is not None:
                body = request_only_content.get(content_key, body)
            if body is None:
                body = self._recover_request_source(
                    session_id,
                    checkpoint_ns,
                    ref,
                    contribution.contribution_id if contribution is not None else None,
                )
            # 在创建 detail manifest 之前完成 source identity 校验。否则一个
            # 错误正文会先被写入并参与 seal，错误只会在后续 dispatch projection
            # 才暴露，留下一个看似 sealed 但不可重放的 assembly。
            body_length = payload_content_length(
                ref.payload_kind or "structured_content",
                body,
            )
            if body_length != ref.content_length:
                raise ValueError(
                    f"source-mismatch: request-only content_length: {ref.ref_id}"
                )
            if contribution is not None:
                if contribution.source_revision != ref.source_revision:
                    raise ValueError(
                        f"source-mismatch: request-only contribution source_revision: {ref.ref_id}"
                    )
                body_hash = contribution_content_hash(
                    contribution.contribution_kind,
                    body,
                )
            else:
                body_hash = sha256_jcs(body)
            if ref.content_hash is not None and body_hash != ref.content_hash:
                raise ValueError(
                    f"source-mismatch: request-only content_hash: {ref.ref_id}"
                )
            pending.append((ref, body))
        registered: list[DetailRef] = []
        detail_by_ref: dict[str, DetailRef] = {}
        try:
            for ref, body in pending:
                record = self._detail_store.write(
                    session_id=session_id,
                    assembly_id=assembly_id,
                    detail_kind="request_source",
                    retention_class="request_replay",
                    visibility=ref.visibility,
                    detail=body,
                    required=True,
                    sensitive=ref.protection != "public",
                    protection=ref.protection,
                    checkpoint_ns=checkpoint_ns,
                    source_revision=ref.source_revision,
                )
                records.append(record)
                if (
                    ref.redacted_stable_digest is not None
                    and record.redacted_stable_digest != ref.redacted_stable_digest
                ):
                    raise ValueError(
                        f"source-mismatch: request-only protected digest: {ref.ref_id}"
                    )
                self._storage.register_context_plan_detail(record)
                registered.append(record.detail_ref)
                detail_by_ref[ref.ref_id] = record.detail_ref
        except BaseException:
            self._storage.mark_context_plan_details_unavailable(
                session_id,
                detail_refs=registered,
                checkpoint_ns=checkpoint_ns,
            )
            for record in records:
                self._detail_store.remove(session_id=session_id, record=record)
            raise
        return plan, tuple(records), detail_by_ref


__all__ = ["ContextProjectionOwnerMixin"]
