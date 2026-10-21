"""Saver context projection 的 source/detail 校验 owner。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from app.abstractions.session_context import SessionContextHistoryProjection
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import (
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import (
    ContextRequestPlan,
    resolve_contribution_for_ref,
)
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.schemas.internal_v2.session_context import SessionContextResourceDTO
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailRecord,
    DetailUnavailableError,
    PreparedDetail,
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
    RolloutReadSnapshot,
)
from app.services.mapping.itemized.projection import (
    ProjectionEvidence,
    build_projection_evidence,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)


@dataclass(frozen=True, slots=True)
class CommittedContextSnapshot:
    """同一读取事务校验的 assembly 与 activation，禁止再读当前 Registry。"""

    assembly: ContextAssemblySnapshot
    resource_activation: ResourceActivationSnapshotRef | None


@dataclass(frozen=True, slots=True)
class ContextProjectionResult:
    """同一封存材料生成的请求、诊断和安全资源摘要；不新增历史成员。"""

    assembly: ContextAssemblySnapshot
    messages: list[BaseMessage]
    tools: list[dict[str, object]]
    losses: tuple[str, ...]
    native: dict[str, object] | None
    resource_refs: tuple[SessionContextResourceDTO, ...]

    def evidence(self, projection: str) -> ProjectionEvidence:
        return build_projection_evidence(
            self.assembly.as_sealed_plan(), projection=projection,
            losses=self.losses, resource_refs=self.resource_refs,
        )


class ContextProjectionOwnerMixin:
    """负责已 seal plan 的正文来源校验和 assembly detail locator 绑定。"""

    @staticmethod
    def _require_context_plan_owner(
        session_id: str, thread_id: str, plan: ContextRequestPlan
    ) -> None:
        """先验证调用方的完整 SessionThread owner。"""
        if (plan.session_id, plan.thread_id) != (session_id, thread_id):
            raise ValueError(
                "source-mismatch: context plan session/thread owner 不一致: "
                f"plan=({plan.session_id},{plan.thread_id}), "
                f"requested=({session_id},{thread_id})"
            )

    def _require_context_plan_read_owner(
        self, session_id: str, thread_id: str, plan: ContextRequestPlan,
        checkpoint_ns: str, *, snapshot: RolloutReadSnapshot | None = None,
    ) -> None:
        """新计划必须属于当前 owner；封存历史只授权目标本地冻结 scope。"""
        if (plan.session_id, plan.thread_id) == (session_id, thread_id):
            return
        if plan.plan_state != "sealed":
            self._require_context_plan_owner(session_id, thread_id, plan)
        try:
            with self._storage.storage.read_connection(
                session_id, thread_id, checkpoint_ns, snapshot=snapshot,
            ) as connection:
                require_historical_scope(
                    connection, plan.session_id, plan.thread_id,
                    current_owner=(session_id, thread_id),
                )
        except RolloutNotMaterializedError as error:
            raise ValueError(
                "source-mismatch: target rollout is not materialized; "
                "frozen history scope cannot be authorized"
            ) from error

    def _validate_snapshot_sources(
        self, snapshot: ContextAssemblySnapshot, checkpoint_ns: str, *,
        items: Mapping[tuple[str, str, str], CanonicalItemRecord],
        prepared_details: tuple[PreparedDetail, ...] = (),
    ) -> None:
        """校验已选正文；摘要候选同时核对冻结的 canonical 来源。"""
        from app.services.mapping.itemized.selection import resolve_selected_item

        entries = tuple(
            entry
            for entry in snapshot.selection
            if entry.ref.ref_type == "canonical_item" and (
                entry.included or snapshot.selection_policy == "summary_candidate"
            )
        )
        for entry in entries:
            resolve_selected_item(entry, items)
        self._request_content_for_plan(
            snapshot.session_id,
            snapshot.thread_id,
            checkpoint_ns,
            snapshot.as_sealed_plan(),
            None,
            prepared_details=prepared_details,
        )

    def project_context_plan_to_native(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """从同一已提交 selection 直接编码 Responses input，不经过 LangChain。"""
        snapshot, items, request_content = self._read_committed_context_projection(
            session_id, thread_id, plan,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
        )
        return self._project_native_context(
            session_id, thread_id, snapshot.assembly, items, request_content,
            resource_refs=(project_sealed_resource_refs(snapshot.resource_activation)
                           if snapshot.resource_activation is not None else ()),
        )

    def _read_committed_context_projection(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str,
        request_only_content: Mapping[str, object] | None = None,
        include_request_only: bool = True,
    ) -> tuple[CommittedContextSnapshot, dict[tuple[str, str, str], CanonicalItemRecord], dict[str, object]]:
        """一次 fresh authority 读取冻结投影材料，关闭连接后才运行各 projector。"""
        if plan.plan_state != "sealed" or plan.assembly_id is None:
            self._require_context_plan_owner(session_id, thread_id, plan)
            raise ValueError("context plan 未 sealed，不能进入 projector")
        self._require_context_plan_read_owner(
            session_id, thread_id, plan, checkpoint_ns,
        )
        with self._storage.storage.open_read_snapshot(
            session_id, thread_id, checkpoint_ns,
        ) as read_snapshot:
            committed_snapshot = self._committed_context_snapshot(
                session_id,
                thread_id,
                plan,
                checkpoint_ns=checkpoint_ns,
                snapshot=read_snapshot,
            )
            committed = committed_snapshot.assembly.as_sealed_plan()
            refs = [
                entry.ref
                for entry in committed.selection
                if (entry.included or committed.selection_policy == "summary_candidate")
                and isinstance(entry.ref, ContextRef)
                and entry.ref.ref_type == "canonical_item"
            ]
            request_content = (
                self._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, committed, request_only_content,
                    snapshot=read_snapshot,
                )
                if include_request_only else {}
            )
            items = self._storage.storage.read_ref_items(
                session_id, thread_id, checkpoint_ns=checkpoint_ns, refs=refs,
                snapshot=read_snapshot,
            )
        return committed_snapshot, items, request_content

    def _project_native_context(
        self,
        session_id: str,
        thread_id: str,
        snapshot: ContextAssemblySnapshot,
        items: Mapping[tuple[str, str, str], CanonicalItemRecord],
        request_content: Mapping[str, object],
        *, resource_refs: tuple[SessionContextResourceDTO, ...],
    ) -> dict[str, object]:
        """只消费本次读取的材料，不自行重读或重新选择 context。"""
        from app.services.infrastructure.rollout_context.provider.native_request import (
            project_native_request,
        )

        native = project_native_request(
            snapshot.as_sealed_plan(), items, request_content,
            seal_binding=snapshot.seal_binding,
            attachment_projector=lambda payload: self._storage.storage.project_attachment_payload(session_id, thread_id, payload),
        )
        native["resource_refs"] = [ref.model_dump(mode="json") for ref in resource_refs]
        return native

    def project_context_plan_to_history_with_diagnostics(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> SessionContextHistoryProjection:
        """从一次权威读取返回 history、loss 与封存资源，不读取 request-only 正文。"""
        committed, items, content = self._read_committed_context_projection(
            session_id, thread_id, plan,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            include_request_only=False,
        )
        projection = self._project_context_projection(
            session_id, thread_id, committed.assembly, items, content,
            resource_activation=committed.resource_activation,
            include_runtime_notices=False, include_summaries=False, target_format=None,
            include_request_only=False, collect_capability_losses=True,
        )
        return SessionContextHistoryProjection(
            assembly=committed.assembly, messages=projection.messages, losses=projection.losses,
            resource_activation=committed.resource_activation,
        )

    def _request_content_for_plan(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        plan: ContextRequestPlan,
        explicit_content: Mapping[str, object] | None,
        *,
        prepared_details: tuple[PreparedDetail, ...] = (),
        snapshot: RolloutReadSnapshot | None = None,
    ) -> dict[str, object]:
        """从 assembly-bound detail 恢复正文；seal 只读显式冻结的内存值。"""
        self._require_context_plan_read_owner(
            session_id, thread_id, plan, checkpoint_ns, snapshot=snapshot,
        )
        if plan.plan_state != "sealed" or plan.assembly_id is None:
            raise ValueError("context plan 未 sealed，不能解析 request-only 正文")
        # sealed identity 不再读取可能属于其它 plan/revision 的实时 source cache。
        # 显式正文仅用于相等性校验，不能替代已提交或本次待提交的 detail。
        prepared_by_ref = {}
        for prepared in prepared_details:
            prepared.record.detail_ref.require_owner(session_id, thread_id, plan.assembly_id)
            if prepared.record.checkpoint_ns != checkpoint_ns:
                raise ValueError("source-mismatch: prepared detail namespace 不一致")
            if prepared.record.detail_ref in prepared_by_ref:
                raise ValueError("source-mismatch: prepared detail identity 重复")
            prepared_by_ref[prepared.record.detail_ref] = prepared
        result: dict[str, object] = {}
        for contribution in plan.contributions:
            if contribution.body is not None:
                result.setdefault(contribution.contribution_id, contribution.body)
        if explicit_content is not None:
            result.update(explicit_content)
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
            detail_ref.require_owner(plan.session_id, plan.thread_id, plan.assembly_id)
            prepared = prepared_by_ref.get(detail_ref)
            detail_record = self._request_source_record(
                session_id, thread_id, checkpoint_ns, detail_ref, ref,
                prepared=prepared, snapshot=snapshot,
            )
            detail_envelope = (
                self._detail_store.read_prepared(
                    prepared, include_sensitive=ref.protection != "public",
                ) if prepared is not None else self._detail_store.read(
                    session_id=session_id, thread_id=thread_id, record=detail_record,
                    include_sensitive=ref.protection != "public",
                    snapshot=snapshot,
                )
            )
            detail_body = detail_envelope.get("detail")
            if (
                detail_body is None
                or detail_envelope.get("protection") != ref.protection
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
            if ref.content_hash is None:
                raise ValueError(f"source-mismatch: local request source 缺少 content_hash: {ref_id}")
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
            expected_token = ref.content_hash
            actual_token = body_hash
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
                contribution.content_hash
            )
            if (
                contribution.content_length
                != payload_content_length("structured_content", body)
                or (
                    contribution_content_hash(contribution.contribution_kind, body)
                )
                != expected_token
            ):
                raise ValueError(
                    f"source-mismatch: context contribution: {contribution_id}"
                )
        return result

    def _request_source_record(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        detail_ref: DetailRef,
        ref: ContextRef,
        *,
        prepared: PreparedDetail | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> DetailRecord:
        """request source 的 typed owner 与用途必须在读取正文前一致。"""
        if prepared is not None:
            detail_ref.require_owner(session_id, thread_id)
            record = prepared.record
        else:
            try:
                raw = self._storage.assemblies.get_context_plan_detail(
                    session_id,
                    thread_id,
                    detail_ref=detail_ref,
                    checkpoint_ns=checkpoint_ns,
                    snapshot=snapshot,
                )
            except KeyError as error:
                raise DetailUnavailableError(
                    f"detail-unavailable: request source detail missing: {ref.ref_id}"
                ) from error
            record = detail_record_from_mapping(raw)
        if (
            record.detail_ref != detail_ref
            or record.checkpoint_ns != checkpoint_ns
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
        self, session_id: str, thread_id: str, checkpoint_ns: str,
        ref: ContextRef, contribution_id: str | None,
    ) -> object:
        """先按 typed locator 冷读；旧封存选择仅查询匹配来源的索引事实。"""
        if isinstance(ref.source_ref, DetailRef):
            record = self._request_source_record(
                session_id, thread_id, checkpoint_ns, ref.source_ref, ref,
            )
            return self._detail_store.read(
                session_id=session_id, thread_id=thread_id, record=record,
                include_sensitive=ref.protection != "public",
            )["detail"]
        with self._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            candidates = connection.execute(
                "SELECT DISTINCT assembly.assembly_id FROM context_assembly_selections selected "
                "JOIN context_assemblies assembly ON assembly.assembly_id=selected.assembly_id "
                "WHERE selected.ref_type='request_only' AND selected.included=1 "
                "AND ((? IS NOT NULL AND selected.contribution_id=?) "
                "OR (? IS NULL AND selected.ref_id=?)) ORDER BY assembly.rowid DESC",
                (contribution_id, contribution_id, contribution_id, ref.ref_id),
            )
            for (assembly_id,) in candidates:
                snapshot = self._storage.assemblies._read_context_assembly_connection(
                    connection, session_id, thread_id, assembly_id=assembly_id,
                    checkpoint_ns=checkpoint_ns,
                )[0]
                for entry in snapshot.selection:
                    previous = entry.ref
                    if not entry.included or previous.ref_type != "request_only":
                        continue
                    matches = entry.contribution_id == contribution_id if contribution_id is not None else (
                        entry.contribution_id is None and (
                            previous.source_ref == ref.source_ref if ref.source_ref is not None
                            else previous.ref_id == ref.ref_id and previous.plan_id == ref.plan_id
                        )
                    )
                    if not matches or any(
                        getattr(previous, field) != getattr(ref, field)
                        for field in (
                            "source_revision", "content_length", "content_hash", "protection",
                            "visibility", "base_delta_role", "source_overlay_epoch",
                            "overlay_from_revision", "overlay_to_revision", "overlay_diff_hash",
                        )
                    ):
                        continue
                    if not isinstance(entry.detail_ref, DetailRef):
                        raise DetailUnavailableError(f"detail-unavailable: recovered source 缺少 typed detail: {ref.ref_id}")
                    entry.detail_ref.require_owner(snapshot.session_id, snapshot.thread_id, snapshot.assembly_id)
                    record = self._request_source_record(
                        session_id, thread_id, checkpoint_ns, entry.detail_ref, ref,
                    )
                    return self._detail_store.read(
                        session_id=session_id, thread_id=thread_id, record=record,
                        include_sensitive=ref.protection != "public",
                    )["detail"]
        raise DetailUnavailableError(f"detail-unavailable: no committed request-only source: {ref.ref_id}")

    def _draft_request_body(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        plan: ContextRequestPlan,
        ref: ContextRef,
        *,
        request_only_content: Mapping[str, object] | None = None,
    ) -> object:
        """编译与 detail binding 共用正向 draft 正文解析和 manifest 校验。"""
        if ref.availability != "available":
            raise DetailUnavailableError(
                f"detail-unavailable: request-only source unavailable: {ref.ref_id}"
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
                (
                    session_id,
                    thread_id,
                    checkpoint_ns,
                    plan.plan_id,
                    content_key,
                ),
                self._request_only_content.get(
                    (session_id, thread_id, checkpoint_ns, content_key)
                ),
            )
        )
        if request_only_content is not None:
            body = request_only_content.get(content_key, body)
        if body is None:
            body = self._recover_request_source(
                session_id,
                thread_id,
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
        return body

    def _bind_request_detail_refs(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        plan: ContextRequestPlan,
        *,
        assembly_id: str,
        omitted_ref_identities: frozenset[tuple[str, ...]] = frozenset(),
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[ContextRequestPlan, tuple[PreparedDetail, ...], dict[tuple[str, ...], DetailRef]]:
        """为本次 sealed assembly 建立 target-local request detail binding。

        omission 和 detail mapping 都以 selection_ref_identity(ref) 为键，
        不把裸 ref_id 当作完整 source identity 或 detail path。每个 included
        request-only entry 都必须拥有同一 assembly 下的 detail_ref；
        contribution-backed entry 另外通过显式
        `contribution_id` 关联 manifest。正文来自当前已登记的实时 body 或
        同一 source identity 的旧 sealed detail，不读取当前源文件，
        也不把正文写入 canonical item。
        """
        self._require_context_plan_owner(session_id, thread_id, plan)
        if plan.plan_state != "unsealed" or plan.assembly_id is not None:
            raise ValueError(
                "plan-order-integrity: 只能为 unsealed plan 分配 detail binding"
            )
        prepared_details: list[PreparedDetail] = []
        pending: list[tuple[ContextRef, object]] = []
        for ref in plan.refs:
            if ref.ref_type != "request_only":
                continue
            if selection_ref_identity(ref) in omitted_ref_identities:
                # omitted entry 只保留 source identity，由 assembly composer
                # 创建 tagged selection；不能为了未发送的 source 生成 detail。
                continue
            if ref.availability != "available":
                # optional omission 由 sealed selection 表达，不分配 detail
                # binding，也不触发正文读取或生成替代详情。
                continue
            body = self._draft_request_body(
                session_id, thread_id, checkpoint_ns, plan, ref,
                request_only_content=request_only_content,
            )
            pending.append((ref, body))
        detail_by_ref: dict[tuple[str, ...], DetailRef] = {}
        for ref, body in pending:
            prepared = self._detail_store.prepare(
                session_id=session_id,
                thread_id=thread_id,
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
            record = prepared.record
            prepared_details.append(prepared)
            detail_by_ref[selection_ref_identity(ref)] = record.detail_ref
        return plan, tuple(prepared_details), detail_by_ref


__all__ = ["CommittedContextSnapshot", "ContextProjectionOwnerMixin", "ContextProjectionResult"]
