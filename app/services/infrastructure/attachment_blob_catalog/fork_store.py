"""附件目录 fork：同工作区关联原身份，跨工作区复制已冻结的完整目录版本。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from app.core.identifier import create_uuid_hex
from app.core.phase_observer import observe_phase
from app.core.resource_uri import (
    file_uri_child,
    file_uri_root,
    parse_attachment_uri,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
from app.core.session_control_store.copy_attachment_settlement import (
    prepare_copy_attachment_settlement,
    read_copy_attachment_settlements,
    transition_copy_attachment_settlement,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.session import SessionForkResourceSourceDTO
from app.services.infrastructure.attachment_blob_catalog.directories import (
    copy_directory_files,
    directory_manifest,
)
from app.services.infrastructure.attachment_blob_catalog.fork_import import (
    ForkAttachmentImportMixin,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_captured_directory,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentDirectorySnapshot,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetContext,
    ResourceTargetFacts,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)


class ForkAttachmentStoreMixin(ForkAttachmentImportMixin):
    def project_attachment(self, target_session_id: str, target_thread_id: str, file_id: str) -> dict[str, str]:
        self._catalog.acquire_access()
        if not self._catalog.database_path.is_file():
            return {"file_id": file_id, "availability": "unavailable"}
        self._assert_session_active(target_session_id, target_thread_id)
        snapshot = self._catalog.fork_attachment_snapshot_for_alias(target_session_id=target_session_id, target_thread_id=target_thread_id, source_file_id=file_id)
        if snapshot is not None:
            if snapshot.availability != "available":
                return {"file_id": file_id, "availability": "unavailable"}
            claim = self._catalog.connection.execute("SELECT state FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
            record = self._catalog.get_directory(snapshot.target_attachment_id)
            parsed = parse_attachment_uri(file_id, require_file=True)
            target_uri = file_uri_root(snapshot.target_file_id)
            projected_file = file_uri_child(target_uri, *parsed.tail_segments[1:]) if parsed.kind == "attachments" else file_uri_child(target_uri, "original", "content")
            return {"file_id": projected_file, "directory_uri": target_uri,
                    "availability": "available" if claim is not None and claim[0] == "committed" and record is not None and record.availability == "available" else "unavailable"}
        parsed = parse_resource_uri(file_id)
        if parsed.kind == "attachments" and parsed.scope_id != self.workspace_id:
            return {"file_id": file_id, "availability": "unavailable"}
        try:
            attachment_id, _ = self._resource_id(file_id, session_id=target_session_id, thread_id=target_thread_id, canonical=True)
        except FileNotFoundError:
            return {"file_id": file_id, "availability": "unavailable"}
        record = self._catalog.get_directory(attachment_id)
        projected_file = self.reference(attachment_id) if parsed.session_artifact_kind == "attachments" else parsed.display_uri
        return {"file_id": projected_file,
                "directory_uri": file_uri_root(projected_file),
                "availability": "available" if record is not None and record.availability == "available" else "unavailable"}

    def _fork_candidates(self, *, source_session_id: str, source_thread_id: str, references: Sequence[tuple[str, bool]]):
        grouped = {}
        for file_id, required in references:
            projected = self.project_attachment(source_session_id, source_thread_id, file_id)
            inherited = self._catalog.fork_attachment_snapshot_for_alias(target_session_id=source_session_id, target_thread_id=source_thread_id, source_file_id=file_id)
            if inherited is not None:
                attachment_id = inherited.target_attachment_id
            else:
                try:
                    attachment_id, _ = self._resource_id(projected["file_id"], session_id=source_session_id, thread_id=source_thread_id, canonical=True)
                except FileNotFoundError:
                    parsed = parse_resource_uri(file_id)
                    attachment_id = parsed.tail_segments[0] if parsed.kind == "attachments" else parsed.tail_segments[2]
            aliases, previous_required, previous = grouped.setdefault(attachment_id, (set(), False, projected))
            if (previous.get("directory_uri"), previous["availability"]) != (projected.get("directory_uri"), projected["availability"]):
                raise RuntimeError("同一附件目录的 alias 对应不同目录归属或可用性")
            aliases.add(file_id)
            grouped[attachment_id] = aliases, previous_required or required, projected
        return grouped

    def project_fork_attachment_sources(self, *, source_session_id: str, source_thread_id: str, references: Sequence[tuple[str, bool]]) -> tuple[SessionForkResourceSourceDTO, ...]:
        result = []
        for attachment_id, (aliases, required, projected) in sorted(self._fork_candidates(source_session_id=source_session_id, source_thread_id=source_thread_id, references=references).items()):
            original = self._catalog.file_version(attachment_id, "original/content")
            result.append(SessionForkResourceSourceDTO(resource_uri=min(aliases), name=original.file_name if original else None, content_type=original.mime_type if original else None,
                                                       availability=projected["availability"], required_for_context=required))
        return tuple(result)

    def capture_fork_attachments(self, record: ForkSourceCaptureRecord, *, references: Sequence[tuple[str, bool]], same_storage_owner: bool) -> tuple[ForkAttachmentDirectorySnapshot, ...]:
        request = json.loads(record.request_json)
        source_domain, target_domain = request["source_storage_domain_id"], request["target_storage_domain_id"]
        if source_domain != self.workspace_id or (source_domain == target_domain) != same_storage_owner:
            raise ValueError("fork_owner_overlap：目录资源域与实际 owner 不一致")
        choices = {choice["resource_uri"]: choice["action"] for choice in request["resources"]}
        candidates = self._fork_candidates(source_session_id=record.source_session_id, source_thread_id=record.source_thread_id, references=references)
        previous = self._catalog.fork_attachment_snapshots(record.copy_operation_id, target_session_id=record.target_session_id, target_thread_id=record.target_thread_id)
        if previous:
            if {snapshot.source_attachment_id: ({snapshot.source_file_id, *snapshot.source_file_aliases}, snapshot.required) for snapshot in previous} != {key: (value[0], value[1]) for key, value in candidates.items()}:
                raise RuntimeError("重复目录 capture 的来源集合与原冻结操作不一致")
            result = previous
        else:
            snapshots = []
            for attachment_id, (aliases, required, projected) in sorted(candidates.items()):
                actions = {choices[alias] for alias in aliases if alias in choices}
                if len(actions) > 1:
                    raise ValueError("同一目录的 alias 不能选择不同继承动作")
                original = self._catalog.file_version(attachment_id, "original/content")
                directory = self._catalog.get_directory(attachment_id)
                available = projected["availability"] == "available" and directory is not None and original is not None
                files, directories = (), ()
                if available and actions != {"omit"}:
                    try:
                        files, directories = self._fork_directory_manifest(attachment_id)
                    except FileNotFoundError:
                        if required:
                            raise
                        available = False
                decision = check_resource_target(ResourceTargetFacts(
                    resource_uri=min(aliases), storage_domain_id=source_domain, lifecycle="workspace", available=available,
                    required_for_context=required, snapshot_import_supported=True,
                ), ResourceTargetContext(storage_domain_id=target_domain, owner_session_id=record.target_session_id, owner_thread_id=record.target_thread_id), choice=next(iter(actions), None))
                if not decision.available:
                    files, directories = (), ()
                target_id = attachment_id if same_storage_owner else "att_" + create_uuid_hex(namespace="attachment_directory")
                target_root_uri = resource_display_uri(scope="workspace", scope_id=target_domain, kind="attachments", tail_segments=(target_id,), gateway_authority=request["target_gateway_id"])
                source_uri = parse_attachment_uri(min(aliases), require_file=True)
                children = source_uri.tail_segments[1:] if source_uri.kind == "attachments" else ("original", "content")
                target_file_id = file_uri_child(target_root_uri, *children) if decision.available else min(aliases)
                claim_id = "aclm_" + hashlib.sha256(f"{record.copy_operation_id}|{attachment_id}".encode()).hexdigest() if decision.available else None
                source_file_id, *other_aliases = sorted(aliases)
                snapshots.append(ForkAttachmentDirectorySnapshot(
                    source_attachment_id=attachment_id, source_file_id=source_file_id, source_file_aliases=tuple(other_aliases),
                    source_session_id=record.source_session_id, source_thread_id=record.source_thread_id,
                    target_attachment_id=target_id, target_file_id=target_file_id,
                    target_session_id=record.target_session_id, target_thread_id=record.target_thread_id,
                    target_created_at=directory.created_at if same_storage_owner and directory is not None else self._clock().isoformat(),
                    file_name=original.file_name if original else None, mime_type=original.mime_type if original else None,
                    files=files, directories=directories, availability="available" if decision.available else "unavailable",
                    required=required, target_action=decision.action, claim_id=claim_id, unavailable_reason=decision.reason,
                ))
            result = tuple(snapshots)
        # 重试仍校验受保护正文，不以过去的 capture 或文件 stat 代替当前完整性。
        if previous:
            for snapshot in result:
                if snapshot.availability == "available":
                    self._fork_directory_manifest(snapshot.source_attachment_id)
        self._catalog.prepare_fork_attachment_claims(copy_operation_id=record.copy_operation_id, source_snapshot_id=record.snapshot_id, operation_preimage_hash=record.preimage_hash, snapshots=result)
        for snapshot in result:
            if snapshot.availability != "available" or snapshot.target_action != "copy_snapshot":
                continue
            directory = self._catalog.require_available_directory(snapshot.source_attachment_id)
            source = resolve_attachment_directory(self._attachments_root, directory.relative_locator)
            capture_root = capture_directory(self._sessions_root, record) / record.source_session_id
            target = resolve_captured_directory(capture_root, snapshot.source_attachment_id)
            with workspace_file_mutation_lock(self._workspace_root):
                if directory_manifest(source) != (snapshot.files, snapshot.directories):
                    raise RuntimeError("附件目录在捕获后发生变化，拒绝混合版本")
                copy_directory_files(source, target, snapshot.files, snapshot.directories)
                if directory_manifest(source) != (snapshot.files, snapshot.directories):
                    raise RuntimeError("附件目录在复制期间发生变化，拒绝混合版本")
        return result

    def _fork_directory_manifest(self, attachment_id: str):
        directory = self._catalog.require_available_directory(attachment_id)
        root = resolve_attachment_directory(self._attachments_root, directory.relative_locator)
        with workspace_file_mutation_lock(self._workspace_root):
            files, directories = directory_manifest(root)
            for file in files:
                if file.relative_path.split("/")[0] in {"original", "preview"}:
                    version = self._catalog.file_version(attachment_id, file.relative_path)
                    if version is None or (version.digest, version.length) != (file.digest, file.length):
                        raise RuntimeError("附件目录中的原件或预览与已登记版本不一致")
            if not any(file.relative_path == "original/content" for file in files):
                raise FileNotFoundError("附件目录缺少原件，不能发布 fork")
            return files, directories

    def release_fork_export(self, capture: ForkSourceCaptureRecord) -> None:
        self._catalog.release_fork_export_claims(capture.copy_operation_id)
    async def reserve_copy_attachments(
        self, capture: ForkSourceCaptureRecord, creation: SessionCreationRecord | ThreadCreationRecord | None,
        snapshots: tuple[ForkAttachmentDirectorySnapshot, ...],
    ) -> None:
        claim_ids = tuple(sorted(item.claim_id for item in snapshots if item.claim_id is not None))
        if not any(item.availability == "available" for item in snapshots):
            return
        async with self._topology_gate.shared(), self._gate.exclusive(capture.target_session_id):
            if isinstance(creation, ThreadCreationRecord):
                if self._path_resolver.catalog_store.get_node(capture.target_session_id).state != "active":
                    raise SessionDeletionPendingError("copy child target Session catalog 已关闭，拒绝 reservation")
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
        async with self._topology_gate.shared(), self._gate.exclusive(capture.target_session_id):
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
