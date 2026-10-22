"""附件 fork 的冻结引用、必要保活与 publication 协调。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
from app.core.session_control_store.copy_attachment_settlement import (
    prepare_copy_attachment_settlement,
    read_copy_attachment_settlements,
    transition_copy_attachment_settlement,
)
from app.core.session_lifecycle_gate import (
    OperationHolderGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.session import (
    SessionForkResourceSourceDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    FORK_TRANSFER_CHUNK_BYTES,
    ForkSourceCaptureDTO,
    SessionForkTransferResultDTO,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
    derive_attachment_id,
)
from app.services.infrastructure.attachment_blob_catalog.fork_import import (
    ForkAttachmentImportMixin,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_blob_path,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetAction,
    ResourceTargetContext,
    ResourceTargetFacts,
)


class ForkAttachmentStoreMixin(ForkAttachmentImportMixin):
    """宿主显式提供唯一 catalog、locator、gate、正文验证及控制库端口。"""

    _catalog: AttachmentBlobCatalog
    _path_resolver: SessionCatalogPathResolver
    _attachments_root: Path
    _gate: SessionLifecycleGate
    _blob_commits: OperationHolderGate
    _parse_logical_file_id: Callable[[str, str], str]
    _assert_session_active: Callable[[str, str], int]
    _open_control_store: Callable[[str], SessionControlStore]
    _verify_blob_content: Callable[[Path, str, int, str], None]
    _commit_staged_blob: Callable[..., None]

    def project_attachment(self, target_session_id: str, target_thread_id: str, file_id: str) -> dict[str, str]:
        """同一次冻结 mapping 读取保留引用和可用性，不修改 immutable rollout。"""
        if not self._catalog.database_path.is_file():
            return {"file_id": file_id, "availability": "unavailable"}
        self._assert_session_active(target_session_id, target_thread_id)
        return self._catalog.project_fork_attachment(
            target_session_id=target_session_id, target_thread_id=target_thread_id,
            source_file_id=file_id, workspace_id=read_workspace_id(self._workspace_root), gateway_id=get_current_gateway_id(),
        )

    def _freeze_fork_attachment_candidates(
        self, *, source_session_id: str, source_thread_id: str,
        target_session_id: str, target_thread_id: str,
        references: Sequence[tuple[str, bool]],
    ) -> list[tuple[ForkAttachmentSnapshot, set[str], bool]]:
        """展示与 capture 共用精确 alias/owner 元数据核验，不建立 claim。"""
        grouped: dict[str, tuple[set[str], bool, ForkAttachmentSnapshot | None]] = {}
        unavailable: dict[str, tuple[ForkAttachmentSnapshot, set[str], bool]] = {}
        for file_id, required in references:
            projected = self.project_attachment(source_session_id, source_thread_id, file_id)
            inherited = self._catalog.fork_attachment_snapshot_for_alias(
                target_session_id=source_session_id, target_thread_id=source_thread_id, source_file_id=file_id,
            )
            if inherited is not None and projected["availability"] == "unavailable":
                previous, aliases, previous_required = unavailable.setdefault(
                    inherited.target_attachment_id, (inherited, set(), False),
                )
                if previous != inherited:
                    raise RuntimeError("fork unavailable 附件 alias 的冻结 metadata 冲突")
                aliases.update((file_id, projected["file_id"]))
                unavailable[inherited.target_attachment_id] = previous, aliases, previous_required or required
                continue
            source_id = self._parse_frozen_source_attachment_id(source_session_id, projected["file_id"])
            aliases, previous_required, previous = grouped.setdefault(source_id, (set(), False, inherited))
            if previous != inherited:
                raise RuntimeError("fork attachment alias 的 backing owner 冻结 metadata 冲突")
            aliases.update((file_id, self.reference(source_session_id, source_id)))
            grouped[source_id] = aliases, previous_required or required, inherited
        source_ids = tuple(sorted(grouped))
        revision, sources = self._catalog.freeze_fork_attachment_sources(source_ids)
        candidates: list[tuple[ForkAttachmentSnapshot, set[str], bool]] = []
        for source_id, (inherited, aliases, required) in sorted(unavailable.items()):
            candidates.append((replace(
                inherited, source_attachment_id=source_id,
                source_owner_ref_id=inherited.target_owner_ref_id,
                source_session_id=source_session_id,
                source_thread_id=inherited.target_owner_thread_id,
                source_item_ref=inherited.target_item_ref,
                target_session_id=target_session_id, target_thread_id=target_thread_id,
                catalog_revision=revision, availability="unavailable", claim_id=None,
            ), aliases, required))
        for source_id, (source, blob) in zip(source_ids, sources, strict=True):
            aliases, required, inherited = grouped[source_id]
            if inherited is None:
                authorized = (source.owner_session_id, source.owner_thread_id) == (source_session_id, source_thread_id)
            else:
                authorized = (source.owner_session_id, source.owner_thread_id, source.owner_ref_id, source.blob_id, source.digest, source.variant) == (
                    source_session_id, inherited.target_owner_thread_id, inherited.target_owner_ref_id,
                    inherited.blob_id, inherited.digest, inherited.variant,
                )
            if not authorized:
                raise RuntimeError("source-mismatch: fork attachment ref 缺失或不属于精确 source owner")
            candidates.append((ForkAttachmentSnapshot(
                source_attachment_id=source_id, source_file_id="", source_owner_ref_id=source.owner_ref_id,
                source_session_id=source.owner_session_id, source_thread_id=source.owner_thread_id,
                target_attachment_id="", target_file_id="", target_owner_ref_id="",
                target_session_id=target_session_id, target_thread_id=target_thread_id,
                target_owner_thread_id=target_thread_id,
                source_item_ref=source.item_ref, target_item_ref=None,
                blob_id=blob.blob_id, digest=blob.digest, length=blob.length, relative_locator=blob.relative_locator,
                variant=source.variant, file_name=source.file_name, mime_type=source.mime_type,
                catalog_revision=revision,
                availability="available" if source.state == "active" and blob.availability == "available" else "unavailable",
                required=required, claim_id=None,
            ), aliases, required))
        return candidates

    def _parse_frozen_source_attachment_id(self, session_id: str, file_id: str) -> str:
        """解析 canonical 历史附件引用；owner 由随后精确 catalog ref 校验。"""
        parsed = parse_session_artifact_uri(file_id)
        workspace_id = read_workspace_id(self._workspace_root)
        if (
            parsed.session_artifact_kind != "attachments"
            or parsed.scope_id != workspace_id
            or parsed.tail_segments[0] != session_id
        ):
            raise ValueError("附件资源引用不属于本工作区 owner")
        local_uri = session_artifact_uri(
            workspace_id, session_id, "attachments", parsed.tail_segments[2],
        )
        return self._parse_logical_file_id(session_id, local_uri)

    def project_fork_attachment_sources(
        self, *, source_session_id: str, source_thread_id: str,
        references: Sequence[tuple[str, bool]],
    ) -> tuple[SessionForkResourceSourceDTO, ...]:
        """只投影来源 metadata；真实目标决策、正文校验与保活仍在 capture。"""
        candidates = self._freeze_fork_attachment_candidates(
            source_session_id=source_session_id, source_thread_id=source_thread_id,
            target_session_id=source_session_id, target_thread_id=source_thread_id,
            references=references,
        )
        actual_uris = {uri for uri, _ in references}
        return tuple(SessionForkResourceSourceDTO(
            resource_uri=min(aliases & actual_uris), name=candidate.file_name,
            content_type=candidate.mime_type,
            availability="available" if candidate.availability == "available" else "unavailable",
            required_for_context=required,
        ) for candidate, aliases, required in candidates)

    def capture_fork_attachments(
        self, record: ForkSourceCaptureRecord, *, references: Sequence[tuple[str, bool]], same_storage_owner: bool,
    ) -> tuple[ForkAttachmentSnapshot, ...]:
        """source guard 内冻结实际 backing owner，事务结束后原子建立必要 claim。"""
        request = json.loads(record.request_json)
        source_domain = request["source_storage_domain_id"]
        target_domain = request["target_storage_domain_id"]
        actual_domain = self._path_resolver.catalog_store.get_node(record.source_session_id).workspace_id
        if source_domain != actual_domain or (source_domain == target_domain) != same_storage_owner:
            raise ValueError("fork_owner_overlap: 冻结附件资源域与实际 owner 不一致")
        choices = {choice["resource_uri"]: choice["action"] for choice in request["resources"]}
        source_main = self._path_resolver.main_thread_id(record.source_session_id)
        target_main = request["target_main_thread_id"]
        candidates = self._freeze_fork_attachment_candidates(
            source_session_id=record.source_session_id, source_thread_id=record.source_thread_id,
            target_session_id=record.target_session_id, target_thread_id=record.target_thread_id,
            references=references,
        )
        snapshots = []
        for candidate, aliases, required in candidates:
            source_file_id, *other_aliases = sorted(aliases)
            actions = {choices[alias] for alias in aliases if alias in choices}
            if len(actions) > 1:
                raise ValueError("同一附件的 alias 不能选择不同继承动作")
            snapshot = self._decide_fork_attachment(
                replace(candidate, source_file_id=source_file_id, source_file_aliases=tuple(other_aliases)),
                source_storage_domain_id=source_domain,
                target_storage_domain_id=target_domain,
                target_gateway_id=request["target_gateway_id"],
                source_main_thread_id=source_main, target_main_thread_id=target_main,
                copy_operation_id=record.copy_operation_id, required=required, choice=next(iter(actions), None),
            )
            if snapshot.availability == "available":
                try:
                    self._verify_fork_attachment(snapshot)
                except FileNotFoundError:
                    if required:
                        raise
                    # 可选历史正文真实缺失时冻结不可用；损坏、越权等错误不降级。
                    snapshot = replace(snapshot, availability="unavailable", required=False, claim_id=None,
                                       target_action="omit", unavailable_reason="附件正文缺失")
            snapshots.append(snapshot)
        result = tuple(sorted(snapshots, key=lambda snapshot: (snapshot.source_attachment_id, snapshot.source_file_id)))
        self._catalog.prepare_fork_attachment_claims(
            copy_operation_id=record.copy_operation_id, source_snapshot_id=record.snapshot_id,
            operation_preimage_hash=record.preimage_hash, snapshots=result,
        )
        return result

    def _decide_fork_attachment(
        self, snapshot: ForkAttachmentSnapshot, *, source_storage_domain_id: str, target_storage_domain_id: str,
        target_gateway_id: str | None,
        source_main_thread_id: str, target_main_thread_id: str, copy_operation_id: str,
        required: bool, choice: ResourceTargetAction | None,
    ) -> ForkAttachmentSnapshot:
        target_id = derive_attachment_id(blob_id=snapshot.blob_id, owner_session_id=snapshot.target_session_id,
                                         owner_thread_id=target_main_thread_id, variant=snapshot.variant)
        existing = (self._catalog.get_reusable_attachment_owner(target_id)
                    if source_storage_domain_id == target_storage_domain_id else None)
        if existing is not None and (existing.blob_id, existing.digest, existing.mime_type) != (
            snapshot.blob_id, snapshot.digest, snapshot.mime_type,
        ):
            existing = None
        if existing is not None:
            self._assert_session_active(existing.owner_session_id, existing.owner_thread_id)
        existing_facts = None if existing is None else ResourceTargetFacts(
            resource_uri=session_artifact_uri(target_storage_domain_id, existing.owner_session_id, "attachments", existing.attachment_id,
                                             gateway_authority=target_gateway_id),
            storage_domain_id=target_storage_domain_id, lifecycle="owner", available=True,
            owner_session_id=existing.owner_session_id, owner_thread_id=existing.owner_thread_id,
            retention_scope="session", revision=existing.blob_id, content_hash=existing.digest,
            content_length=snapshot.length,
        )
        source_retains = (snapshot.source_thread_id == source_main_thread_id
                          and self._catalog.get_reusable_attachment_owner(snapshot.source_attachment_id) is not None)
        decision = check_resource_target(
            ResourceTargetFacts(
                resource_uri=snapshot.source_file_id, storage_domain_id=source_storage_domain_id,
                lifecycle="owner", available=snapshot.availability == "available", required_for_context=required,
                snapshot_import_supported=True, owner_session_id=snapshot.source_session_id,
                owner_thread_id=snapshot.source_thread_id, retention_scope="session" if source_retains else "thread",
                revision=snapshot.blob_id, content_hash=snapshot.digest, content_length=snapshot.length,
            ),
            ResourceTargetContext(storage_domain_id=target_storage_domain_id,
                                  owner_session_id=snapshot.target_session_id, owner_thread_id=snapshot.target_thread_id,
                                  existing_reference=existing_facts),
            choice=choice,
        )
        if decision.action == "reuse_reference":
            if existing is None:
                raise RuntimeError("fork attachment reuse_reference 缺少目标 Session 的实际保活引用")
            target_id, target_owner_ref, target_item_ref = existing.attachment_id, existing.owner_ref_id, existing.item_ref
            target_owner_thread = existing.owner_thread_id
            claim_id = None
        else:
            target_id = derive_attachment_id(blob_id=snapshot.blob_id, owner_session_id=snapshot.target_session_id,
                                             owner_thread_id=snapshot.target_thread_id, variant=snapshot.variant)
            target_owner_ref, target_item_ref = "oref_" + target_id.removeprefix("att_"), None
            target_owner_thread = snapshot.target_thread_id
            claim_id = ("fac_" + hashlib.sha256(
                f"{copy_operation_id}|{snapshot.source_owner_ref_id}|{target_owner_ref}".encode()
            ).hexdigest()[:32] if decision.available else None)
        return replace(snapshot, target_attachment_id=target_id, target_owner_ref_id=target_owner_ref,
                       target_file_id=session_artifact_uri(target_storage_domain_id, snapshot.target_session_id, "attachments", target_id,
                                                          gateway_authority=target_gateway_id),
                       target_item_ref=target_item_ref, target_owner_thread_id=target_owner_thread,
                       required=required and decision.available, claim_id=claim_id,
                       availability="available" if decision.available else "unavailable",
                       target_action=decision.action, unavailable_reason=decision.reason)

    def _verify_fork_attachment(self, snapshot: ForkAttachmentSnapshot) -> None:
        blob = self._catalog.get_blob(snapshot.blob_id)
        if (blob.digest, blob.length, blob.availability) != (snapshot.digest, snapshot.length, "available"):
            raise RuntimeError("fork attachment 正文身份或 availability 与冻结回执不一致")
        path = resolve_blob_path(self._attachments_root, blob.relative_locator)
        if path.is_symlink():
            raise RuntimeError(f"fork attachment 正文不得是符号链接: blob_id={snapshot.blob_id}, path={path}")
        self._verify_blob_content(path, snapshot.digest, snapshot.length, snapshot.blob_id)

    def export_fork_attachment_chunk(self, capture: ForkSourceCaptureRecord, blob_id: str, offset: int) -> SessionForkTransferResultDTO:
        snapshots = tuple(snapshot for snapshot in self._catalog.fork_attachment_snapshots(
            capture.copy_operation_id, target_session_id=capture.target_session_id, target_thread_id=capture.target_thread_id,
        ) if snapshot.blob_id == blob_id and snapshot.availability == "available" and snapshot.target_action == "copy_snapshot")
        if not snapshots or offset > snapshots[0].length:
            raise ValueError("fork 附件分块不属于冻结的显式 copy_snapshot 或 bytes offset 越界")
        snapshot = snapshots[0]
        if snapshot.claim_id not in self._catalog.fork_attachment_claim_ids(capture.copy_operation_id):
            raise RuntimeError("fork 附件导出缺少精确 source 保活 claim")
        self._verify_fork_attachment(snapshot)
        blob = self._catalog.get_blob(blob_id)
        with resolve_blob_path(self._attachments_root, blob.relative_locator).open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(FORK_TRANSFER_CHUNK_BYTES)
        return SessionForkTransferResultDTO(capture=ForkSourceCaptureDTO.from_record(capture), offset=offset,
                                            chunk_base64=base64.b64encode(chunk).decode("ascii"),
                                            chunk_sha256=hashlib.sha256(chunk).hexdigest())

    def release_fork_export(self, capture: ForkSourceCaptureRecord) -> None:
        """只消费 fork owner 已核实的独立 source export，不按域标签推导角色。"""
        self._catalog.release_fork_export_claims(capture.copy_operation_id)

    async def reserve_copy_attachments(
        self, capture: ForkSourceCaptureRecord, creation: SessionCreationRecord | ThreadCreationRecord | None,
        snapshots: tuple[ForkAttachmentSnapshot, ...],
    ) -> None:
        claim_ids = tuple(sorted(item.claim_id for item in snapshots if item.claim_id is not None))
        if not any(item.availability == "available" for item in snapshots):
            return
        async with self._gate.exclusive(capture.target_session_id):
            if isinstance(creation, ThreadCreationRecord):
                directory = self._path_resolver.resolve_session_node(capture.target_session_id)
            elif creation is None:
                self._assert_session_active(capture.target_session_id, capture.target_thread_id)
                directory = self._path_resolver.resolve_session_node(capture.target_session_id)
            else:
                directory = self._path_resolver.resolve_prepared_session_node(creation)
            control = SessionControlStore(directory / "session-control.sqlite")
            try:
                if isinstance(creation, ThreadCreationRecord):
                    current = control.get_thread_creation_record(creation.thread_creation_idempotency_key)
                    if current != creation or current.child_thread_id != capture.target_thread_id or current.state != "preparing":
                        raise RuntimeError("fork attachment reservation 与精确 preparing child intent 不一致")
                elif creation is not None:
                    control.verify_matches_catalog_main_thread(capture.target_thread_id)
                else:
                    control.get_thread_graph_binding(capture.target_thread_id)
                if control.get_fence()[0] != "active":
                    raise SessionDeletionPendingError("copy target fence 已关闭，拒绝 reservation")
                self._catalog.reserve_fork_attachment_owners(copy_operation_id=capture.copy_operation_id, claim_ids=claim_ids)
                if claim_ids:
                    prepare_copy_attachment_settlement(
                        control, copy_operation_id=capture.copy_operation_id, preimage_hash=capture.preimage_hash,
                        target_session_id=capture.target_session_id, target_thread_id=capture.target_thread_id,
                        publication_preimage_hash=creation.preimage_hash if creation is not None else capture.preimage_hash,
                        claim_ids=claim_ids,
                    )
            finally:
                control.close()
        if claim_ids:
            await observe_phase(capture.copy_operation_id, "copy.attachments.owner_reserved")

    async def finalize_copy_attachments(
        self, capture: ForkSourceCaptureRecord, *, publication_preimage_hash: str,
    ) -> None:
        """publication 后独立取得 target gate；两个数据库事务顺序提交，不重叠。"""
        claim_ids = self._catalog.fork_attachment_claim_ids(capture.copy_operation_id)
        if claim_ids:
            await observe_phase(capture.copy_operation_id, "copy.attachments.before_finalize")
        async with self._gate.exclusive(capture.target_session_id):
            generation = self._assert_session_active(capture.target_session_id, capture.target_thread_id)
            # omit、不可用或 main 引用复用也必须由同一个目标 gate 决定发布先后。
            if not claim_ids:
                return
            control = self._open_control_store(capture.target_session_id)
            try:
                records = {item.copy_operation_id: item for item in read_copy_attachment_settlements(control)}
                record = records.get(capture.copy_operation_id)
                if record is None:
                    raise RuntimeError("copy attachment finalizer 缺少原 publication settlement")
                if (record.target_session_id, record.target_thread_id, record.preimage_hash,
                    record.publication_preimage_hash, record.captured_generation, record.claim_ids) != (
                    capture.target_session_id, capture.target_thread_id, capture.preimage_hash,
                    publication_preimage_hash, generation, claim_ids,
                ) or record.state in {"aborted", "target_deleted"}:
                    raise RuntimeError("copy attachment finalizer owner/generation/publication 前像冲突")
                if record.state == "preparing":
                    record = transition_copy_attachment_settlement(control, record, state="published_pending_attachment_commit")
                if record.state == "committed":
                    return
                await observe_phase(capture.copy_operation_id, "copy.attachments.published_pending")
                self._catalog.settle_fork_attachment_claims(copy_operation_id=capture.copy_operation_id, claim_ids=claim_ids, state="committed")
                transition_copy_attachment_settlement(control, record, state="committed")
                await observe_phase(capture.copy_operation_id, "copy.attachments.claims_committed")
            finally:
                control.close()

    def copy_resource_results(self, capture: ForkSourceCaptureRecord) -> list[dict[str, object]]:
        """读取冻结资源决策；响应必须完成原 finalizer，正文读取仍独立验证实时 owner。"""
        results = []
        for snapshot in self._catalog.fork_attachment_snapshots(
            capture.copy_operation_id, target_session_id=capture.target_session_id, target_thread_id=capture.target_thread_id,
        ):
            results.append({
                "resource_uri": snapshot.source_file_id, "action": snapshot.target_action,
                "availability": snapshot.availability,
                "target_uri": snapshot.target_file_id if snapshot.availability == "available" else None,
                "reason": snapshot.unavailable_reason if snapshot.availability == "unavailable" else None,
            })
        return results

    def mark_copy_published(self, capture: ForkSourceCaptureRecord, *, publication_preimage_hash: str) -> None:
        """publication 持有同一 target gate 时调用；通知失败不倒删已发布 target。"""
        if not self._catalog.fork_attachment_claim_ids(capture.copy_operation_id):
            return
        control = self._open_control_store(capture.target_session_id)
        try:
            for record in read_copy_attachment_settlements(control):
                if record.copy_operation_id != capture.copy_operation_id:
                    continue
                if record.publication_preimage_hash != publication_preimage_hash:
                    raise RuntimeError("copy publication preimage 与 settlement 不一致")
                if record.state == "preparing":
                    transition_copy_attachment_settlement(control, record, state="published_pending_attachment_commit")
                elif record.state not in {"published_pending_attachment_commit", "committed"}:
                    raise RuntimeError("copy publication 不得复活 terminal target")
        finally:
            control.close()

    async def abort_copy_attachments(self, capture: ForkSourceCaptureRecord, creation: SessionCreationRecord | ThreadCreationRecord) -> None:
        if isinstance(creation, ThreadCreationRecord):
            directory = self._path_resolver.resolve_session_node(capture.target_session_id)
            control = SessionControlStore(directory / "session-control.sqlite")
            try:
                current = control.get_thread_creation_record(creation.thread_creation_idempotency_key)
                if current != creation or current.state != "preparing" or current.purpose != "fork" or current.child_thread_id != capture.target_thread_id:
                    raise RuntimeError("fork attachment abort 缺少精确 preparing child creation capability")
            finally:
                control.close()
        else:
            directory = self._path_resolver.resolve_prepared_session_node(creation)
        claim_ids = self._catalog.fork_attachment_claim_ids(capture.copy_operation_id)
        self._catalog.settle_fork_attachment_claims(copy_operation_id=capture.copy_operation_id, claim_ids=claim_ids, state="aborted")
        control = SessionControlStore(directory / "session-control.sqlite")
        try:
            for record in read_copy_attachment_settlements(control):
                if record.copy_operation_id == capture.copy_operation_id and record.state == "preparing":
                    transition_copy_attachment_settlement(control, record, state="aborted")
        finally:
            control.close()
        await self.collect_fork_import_staging(capture.copy_operation_id)
