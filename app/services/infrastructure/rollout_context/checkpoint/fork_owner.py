"""Checkpoint fork 的显式 source/copy/recovery 编排入口。"""


from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace

from app.abstractions.session_context import SessionForkTransportProtocol
from app.core.identifier import create_uuid_hex
from app.core.phase_observer import observe_phase
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_store import ThreadCreationRecord
from app.core.session_lifecycle_gate import NavigationTopologyGate, SessionLifecycleGate
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionForkRequest
from app.services.infrastructure.rollout_context.checkpoint.fork_copy import (
    CheckpointForkCopy,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_recovery import (
    CheckpointForkRecovery,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
    RolloutForkResult,
)
from app.services.infrastructure.rollout_context.fork.snapshot_copy import (
    finalize_target_snapshot_publication,
    recover_target_snapshot_publication,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
    read_captured_snapshot,
    remove_captured_snapshot,
)
from app.services.infrastructure.rollout_context.fork.transfer import (
    import_captured_artifact,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


class CheckpointForkOwner:
    def __init__(
        self, *, storage, source: CheckpointForkSource,
        copy: CheckpointForkCopy, recovery: CheckpointForkRecovery,
    ) -> None:
        self._storage = storage
        self.source = source
        self.copy = copy
        self.recovery = recovery

    def copy_resource_results(self, capture: ForkSourceCaptureRecord, *, target_thread_uri: str) -> list[dict[str, object]]:
        """结果从各资源 owner 的持久发布证据读取，重试不读取来源。"""
        resources = self.source.attachment_store.copy_resource_results(capture) if self.source.attachment_store is not None else []
        request = json.loads(capture.request_json)
        journal = self._storage.forks.read_fork_debug_snapshot(
            capture.materialization_id, target_session_id=capture.target_session_id,
            target_thread_id=capture.target_thread_id, checkpoint_ns=request["checkpoint_ns"],
        )
        mapping = {}
        if journal is not None:
            state, proof = journal
            if state != "published":
                raise RuntimeError("fork result 的调试方案尚未完整发布")
            mapping = dict(proof["target_configuration_id_map"])
        prefix = request["source_uri"] + "/debug/node/configurations/"
        for choice in request["resources"]:
            if not choice["resource_uri"].startswith(prefix):
                continue
            configuration_id = choice["resource_uri"][len(prefix):]
            target_id = mapping.get(configuration_id)
            if choice["action"] == "copy_snapshot" and target_id is None:
                raise RuntimeError("fork result 缺少显式调试方案的 publication proof")
            resources.append({
                "resource_uri": choice["resource_uri"], "action": choice["action"],
                "availability": "available" if target_id is not None else "unavailable",
                "target_uri": target_thread_uri + "/debug/node/configurations/" + target_id if target_id is not None else None,
                "reason": None if target_id is not None else "用户未继承该静态方案",
            })
        return resources

    async def afork(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        source_uri: str,
        source_owner: MutationIntentOwner | None = None,
        target_owner: MutationIntentOwner | None = None,
        creation_record: SessionCreationRecord | None = None,
        thread_creation_record: ThreadCreationRecord | None = None,
        fork_id: str | None = None,
        materialization_id: str | None = None,
        mode: str = "context_fork",
        anchor_mode: str = "inclusive",
        relationship: str = "detached",
        checkpoint_ns: str = "",
        source_preflight: RolloutForkResult | None = None,
        source_view_id: str | None = None,
        source_view_revision: int | None = None,
        anchor_item_id: str | None = None,
        resource_choices: list[dict[str, object]] | None = None,
        source_storage_domain_id: str | None = None,
        target_storage_domain_id: str | None = None,
        source_transport: SessionForkTransportProtocol | None = None,
        transfer_request: SessionForkRequest | None = None,
    ) -> RolloutForkResult:
        """先冻结全部 source facts，再只从私有快照物化 target。"""
        if creation_record is not None and thread_creation_record is not None:
            raise ValueError("fork target 只能有一个 creation owner")
        if thread_creation_record is not None:
            if thread_creation_record.purpose != "fork" or thread_creation_record.state != "preparing":
                raise RuntimeError("fork child target 必须是本次 preparing creation intent")
            prepared_owner = MutationIntentOwner(session_id=target_session_id, thread_id=thread_creation_record.child_thread_id)
            if target_owner is not None and target_owner != prepared_owner:
                raise RuntimeError("fork child target owner 与 creation intent 不匹配")
            target_owner = prepared_owner
            with self._storage.storage.prepared_child_fork_target_scope(target_session_id, thread_creation_record):
                pass
        elif creation_record is None:
            target_owner = self.source._require_thread_owner(
                target_session_id, target_owner
            )
        else:
            if creation_record.session_id != target_session_id:
                raise RuntimeError("prepared fork creation record target 不匹配")
            prepared_owner = self.source.resolve_prepared_main_thread_owner(creation_record)
            if target_owner is not None and target_owner != prepared_owner:
                raise RuntimeError(
                    "fork prepared target owner 与 creation record 不匹配"
                )
            target_owner = prepared_owner
        catalog = self._storage.forks._catalog_store()
        creation_key = (
            creation_record.session_creation_idempotency_key if creation_record is not None
            else thread_creation_record.thread_creation_idempotency_key if thread_creation_record is not None else None
        )
        operation_id = creation_key or (fork_id if fork_id is not None else create_uuid_hex(namespace="fork/operation"))
        source_external = source_transport is not None
        if source_external and (transfer_request is None or source_owner is None):
            raise ValueError("跨端 fork 必须提供精确 source owner 和冻结 transfer request")
        if source_external and relationship == "pinned":
            raise ValueError("fork_remote_retention_unsupported: 来源端未提供独立 pinned retention 授权")
        capture = catalog.get_fork_source_capture(copy_operation_id=operation_id)
        if capture is None:
            if not source_external:
                source_owner = self.source._require_thread_owner(source_session_id, source_owner)
        else:
            frozen_owner = MutationIntentOwner(session_id=capture.source_session_id, thread_id=capture.source_thread_id)
            if source_owner is not None and source_owner != frozen_owner:
                raise RuntimeError("fork recovery source owner 与冻结 capture 不一致")
            source_owner = frozen_owner
        frozen_request = json.loads(capture.request_json) if capture is not None else None
        source_domain = source_storage_domain_id or (frozen_request["source_storage_domain_id"] if frozen_request is not None
                                                    else catalog.get_node(source_session_id).workspace_id)
        target_domain = target_storage_domain_id or (frozen_request["target_storage_domain_id"] if frozen_request is not None else source_domain)
        capture = catalog.reserve_fork_source_capture(
            copy_operation_id=operation_id, creation_key=creation_key,
            source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
            target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
            fork_id=strict_text(fork_id, field="fork_id") if fork_id is not None else None,
            materialization_id=strict_text(materialization_id, field="materialization_id") if materialization_id is not None else None,
            source_external=source_external,
            request={"source_uri": strict_text(source_uri, field="source_uri"),
                     "source_view_id": source_view_id, "source_view_revision": source_view_revision,
                     "anchor_item_id": anchor_item_id,
                     "mode": mode, "anchor_mode": anchor_mode, "relationship": relationship,
                     "checkpoint_ns": checkpoint_ns,
                      "creation_kind": "new_session_main" if creation_record is not None else "new_child_thread" if thread_creation_record is not None else "existing_empty_thread",
                      "resources": resource_choices or [],
                       "source_storage_domain_id": source_domain,
                       "target_storage_domain_id": target_domain,
                       "target_main_thread_id": target_owner.thread_id if creation_record is not None
                       else self.source.resolve_main_thread_owner(target_session_id).thread_id,
                      "creation_preimage_hash": creation_record.preimage_hash if creation_record is not None else thread_creation_record.preimage_hash if thread_creation_record is not None else None},
        )
        if capture.state == "aborted":
            raise RuntimeError("fork source capture 已 aborted，拒绝复用")
        fork_id, materialization_id = capture.fork_id, capture.materialization_id
        if relationship == "pinned" and capture.state != "materialized":
            await self.recovery.begin_pinned_fork_retention_claim(capture)
            await observe_phase(operation_id, "fork.retention.claimed", details={
                "fork_id": fork_id, "source_session_id": source_session_id,
                "target_session_id": target_session_id,
            })
        target_scope = (
            self._storage.storage.prepared_fork_target_scope(creation_record)
            if creation_record is not None else self._storage.storage.prepared_child_fork_target_scope(target_session_id, thread_creation_record)
            if thread_creation_record is not None else nullcontext()
        )
        with target_scope:
            if capture.state == "materialized":
                with self._storage.storage._connect(target_owner.thread_id, checkpoint_ns,
                                                   session_id=target_session_id, read_only=True) as connection:
                    row = connection.execute(
                        "SELECT source_checkpoint_id,source_view_id,status,source_snapshot_sha256 "
                        "FROM fork_materializations WHERE materialization_id=? AND target_session_id=? AND target_thread_id=? AND checkpoint_ns=?",
                        (capture.materialization_id, target_session_id, target_owner.thread_id, checkpoint_ns),
                    ).fetchone()
                if row is None or row[2:] != ("committed", capture.snapshot_sha256):
                    raise RuntimeError("fork materialized capture 缺少精确 target committed journal")
                frozen = json.loads(capture.request_json)
                return RolloutForkResult(
                    source_checkpoint_id=row[0], source_view_id=row[1],
                    source_view_revision=frozen["source_view_revision"], anchor_item_id=frozen["anchor_item_id"],
                    fork_id=capture.fork_id, materialization_id=capture.materialization_id,
                    journal_state="committed", source_snapshot_sha256=capture.snapshot_sha256,
                )
            root = capture_directory(self._storage.storage.sessions_dir, capture)
            if root.exists() or root.is_symlink():
                source_snapshot, marker_hash = read_captured_snapshot(self._storage.storage.sessions_dir, capture)
            else:
                if capture.state != "preparing":
                    raise RuntimeError("fork captured journal 缺少冻结 snapshot，禁止读取当前 source 补齐")
                root.mkdir(parents=True, exist_ok=False, mode=0o700)
                if source_external:
                    source_snapshot, marker_hash = await import_captured_artifact(
                        source_transport, transfer_request, self._storage.storage.sessions_dir, capture,
                    )
                else:
                    source_snapshot = await self.source._capture_fork_source_snapshot(
                        source_session_id=source_session_id, source_owner=source_owner, mode=mode,
                        checkpoint_ns=checkpoint_ns, snapshot_root=root, capture_record=capture,
                    )
                source_snapshot, marker_hash = read_captured_snapshot(self._storage.storage.sessions_dir, capture)
            if source_external and source_snapshot.attachments:
                if self.source.attachment_store is None:
                    raise RuntimeError("fork import 附件缺少目标 AttachmentBlobStore owner")
                await self.source.attachment_store.accept_fork_import(
                    replace(capture, snapshot_sha256=source_snapshot.snapshot_sha256, marker_sha256=marker_hash),
                    source_snapshot.attachments, transport=source_transport, request=transfer_request,
                )
            await observe_phase(operation_id, "copy.source.before_captured_cas")
            capture = catalog.mark_fork_source_captured(capture, snapshot_sha256=source_snapshot.snapshot_sha256, marker_sha256=marker_hash)
            await observe_phase(operation_id, "copy.source.captured")
            result = await self.copy._afork_from_snapshot(
                source_session_id=source_session_id,
                source_uri=source_uri,
                target_session_id=target_session_id,
                source_owner=source_owner,
                target_owner=target_owner,
                source_snapshot=source_snapshot,
                fork_id=fork_id,
                materialization_id=materialization_id,
                mode=mode,
                anchor_mode=anchor_mode,
                relationship=relationship,
                checkpoint_ns=checkpoint_ns,
                source_preflight=source_preflight,
                source_view_id=source_view_id,
                source_view_revision=source_view_revision,
                anchor_item_id=anchor_item_id,
                capture_record=capture,
                creation_record=creation_record or thread_creation_record,
            )
            await observe_phase(operation_id, "fork.target_committed", details={
                "fork_id": fork_id, "source_session_id": source_session_id,
                "target_session_id": target_session_id,
                "target_index_relative_locator": str(self._storage.storage.index_path(
                    target_session_id, checkpoint_ns, thread_id=target_owner.thread_id,
                ).relative_to(self._storage.storage.sessions_dir.parent)),
            })
            if relationship == "pinned":
                await self.recovery.activate_pinned_fork_retention(capture)
            await asyncio.to_thread(
                self._storage.forks.complete_fork_materialization,
                materialization_id=materialization_id,
                target_session_id=target_session_id,
                target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns,
            )
            await asyncio.to_thread(
                finalize_target_snapshot_publication, self._storage.storage,
                target_session_id=target_session_id, target_thread_id=target_owner.thread_id,
                checkpoint_ns=checkpoint_ns, materialization_id=materialization_id,
            )
            catalog.finish_fork_source_capture(operation_id, state="materialized")
            if creation_record is None and thread_creation_record is None and self.source.attachment_store is not None:
                await self.source.attachment_store.finalize_copy_attachments(capture, publication_preimage_hash=capture.preimage_hash)
            remove_captured_snapshot(self._storage.storage.sessions_dir, capture)
            return replace(result, journal_state="committed")

    async def export_source_capture(self, expected: ForkSourceCaptureRecord) -> ForkSourceCaptureRecord:
        """来源 owner 按目标冻结 identity 导出既有 artifact，重试不回读 live source。"""
        catalog = self._storage.forks._catalog_store()
        request = json.loads(expected.request_json)
        capture = catalog.reserve_fork_source_capture(
            copy_operation_id=expected.copy_operation_id, creation_key=expected.creation_key,
            source_session_id=expected.source_session_id, source_thread_id=expected.source_thread_id,
            target_session_id=expected.target_session_id, target_thread_id=expected.target_thread_id,
            request=request, fork_id=expected.fork_id, materialization_id=expected.materialization_id,
            snapshot_id=expected.snapshot_id, target_external=True,
        )
        if capture.state not in {"preparing", "source_captured"}:
            raise RuntimeError("fork source export 已终结，拒绝重新捕获")
        root = capture_directory(self._storage.storage.sessions_dir, capture)
        if not root.exists():
            if capture.state != "preparing":
                raise RuntimeError("fork export captured artifact 丢失，禁止回源补齐")
            root.mkdir(parents=True, exist_ok=False, mode=0o700)
            try:
                await self.source._capture_fork_source_snapshot(
                    source_session_id=capture.source_session_id,
                    source_owner=MutationIntentOwner(session_id=capture.source_session_id, thread_id=capture.source_thread_id),
                    mode=request["mode"], checkpoint_ns=request["checkpoint_ns"], snapshot_root=root, capture_record=capture,
                )
            except Exception:
                catalog.finish_fork_source_capture(capture.copy_operation_id, state="aborted", reason="source export capture failed")
                if self.source.attachment_store is not None:
                    self.source.attachment_store.release_fork_export(capture)
                remove_captured_snapshot(self._storage.storage.sessions_dir, capture)
                raise
        snapshot, marker_hash = read_captured_snapshot(self._storage.storage.sessions_dir, capture)
        return catalog.mark_fork_source_captured(capture, snapshot_sha256=snapshot.snapshot_sha256, marker_sha256=marker_hash)

    async def release_source_export(self, capture: ForkSourceCaptureRecord, *, committed: bool) -> ForkSourceCaptureRecord:
        """只释放本导出的 artifact/附件 claim；目标生命周期由目标 journal 决定。"""
        catalog = self._storage.forks._catalog_store()
        if committed and capture.state not in {"source_captured", "materialized"}:
            raise RuntimeError("fork source export 未完整 captured，不能接受 committed 回执")
        catalog.finish_fork_source_capture(capture.copy_operation_id, state="materialized" if committed else "aborted")
        if self.source.attachment_store is not None:
            self.source.attachment_store.release_fork_export(capture)
        remove_captured_snapshot(self._storage.storage.sessions_dir, capture)
        return catalog.get_fork_source_capture(copy_operation_id=capture.copy_operation_id)

    def read_child_fork_completion(self, target_session_id: str, record: ThreadCreationRecord) -> dict[str, str]:
        """精确 target journal 是 child 发布/失败清理的正文归属证明。"""
        return self._read_child_fork_proof(target_session_id, record, completed=True)

    def _read_child_fork_proof(self, target_session_id: str, record: ThreadCreationRecord, *, completed: bool) -> dict[str, str]:
        capture = self._storage.forks._catalog_store().get_fork_source_capture(
            creation_key=record.thread_creation_idempotency_key,
        )
        if capture is None or capture.snapshot_sha256 is None or (completed and capture.state != "materialized"):
            raise RuntimeError("fork child 缺少本 operation 的 materialized capture")
        request = json.loads(capture.request_json)
        if (capture.target_session_id, capture.target_thread_id, request["creation_preimage_hash"]) != (
            target_session_id, record.child_thread_id, record.preimage_hash,
        ):
            raise RuntimeError("fork child completion 与冻结 creation owner 不一致")
        with (
            self._storage.storage.prepared_child_fork_target_scope(target_session_id, record),
            self._storage.storage._connect(
                record.child_thread_id, request["checkpoint_ns"], session_id=target_session_id, read_only=True,
            ) as connection,
        ):
            row = connection.execute(
                "SELECT fork_id,source_session_id,source_thread_id,target_session_id,target_thread_id,"
                "source_view_id,fork_mode,relationship,status,source_snapshot_sha256 "
                "FROM fork_materializations WHERE materialization_id=? AND checkpoint_ns=?",
                (capture.materialization_id, request["checkpoint_ns"]),
            ).fetchone()
        if row is None or row[:8] + row[9:] != (
            capture.fork_id, capture.source_session_id, capture.source_thread_id,
            target_session_id, record.child_thread_id, request["source_view_id"],
            request["mode"], request["relationship"], capture.snapshot_sha256,
        ) or row[8] not in ({"committed"} if completed else {"prepared", "target_committed", "committed"}):
            raise RuntimeError("fork child 缺少精确 target committed journal/快照证明")
        return {"fork_id": capture.fork_id, "materialization_id": capture.materialization_id,
                "source_snapshot_sha256": capture.snapshot_sha256,
                "target_session_id": target_session_id, "target_thread_id": record.child_thread_id}

    def mark_published_child_fork(self, target_session_id: str, record: ThreadCreationRecord) -> None:
        proof = self.read_child_fork_completion(target_session_id, record)
        if record.fork_completion_json is None or json.loads(record.fork_completion_json) != proof:
            raise RuntimeError("fork child publication completion 已漂移")
        capture = self._storage.forks._catalog_store().get_fork_source_capture(creation_key=record.thread_creation_idempotency_key)
        if capture is not None and self.source.attachment_store is not None:
            self.source.attachment_store.mark_copy_published(capture, publication_preimage_hash=record.preimage_hash)

    async def finalize_published_child_fork(self, target_session_id: str, record: ThreadCreationRecord) -> None:
        if record.state != "published":
            raise RuntimeError("fork child 附件只能在真实 catalog publication 后终结")
        self.read_child_fork_completion(target_session_id, record)
        capture = self._storage.forks._catalog_store().get_fork_source_capture(creation_key=record.thread_creation_idempotency_key)
        if capture is not None and self.source.attachment_store is not None:
            await self.source.attachment_store.finalize_copy_attachments(capture, publication_preimage_hash=record.preimage_hash)

    async def abort_prepared_child_fork(self, target_session_id: str, record: ThreadCreationRecord) -> bool:
        if record.state != "preparing" or record.purpose != "fork":
            raise RuntimeError("fork child 清理缺少原 preparing creation intent")
        catalog = self._storage.forks._catalog_store()
        capture = catalog.get_fork_source_capture(creation_key=record.thread_creation_idempotency_key)
        if capture is None:
            return False
        request = json.loads(capture.request_json)
        if (capture.target_session_id, capture.target_thread_id, request["creation_preimage_hash"]) != (
            target_session_id, record.child_thread_id, record.preimage_hash,
        ):
            raise RuntimeError("fork child 清理与冻结 capture owner 冲突")
        with self._storage.storage.prepared_child_fork_target_scope(target_session_id, record):
            storage = self._storage.storage
            target_root = storage.root(target_session_id, request["checkpoint_ns"], thread_id=record.child_thread_id)
            if target_root.exists() or target_root.with_name(f".fork-{capture.materialization_id}.rollout").exists() or target_root.with_name(f".fork-{capture.materialization_id}.rollback").exists():
                recover_target_snapshot_publication(storage, target_session_id=target_session_id,
                                                    target_thread_id=record.child_thread_id,
                                                    checkpoint_ns=request["checkpoint_ns"], materialization_id=capture.materialization_id)
            has_rollout = target_root.exists()
            if has_rollout:
                proof = self._read_child_fork_proof(target_session_id, record, completed=False)
                if record.fork_completion_json is not None and json.loads(record.fork_completion_json) != proof:
                    raise RuntimeError("fork child 中止的冻结 completion 与真实 journal 不一致")
                finalize_target_snapshot_publication(storage, target_session_id=target_session_id,
                                                     target_thread_id=record.child_thread_id,
                                                     checkpoint_ns=request["checkpoint_ns"], materialization_id=capture.materialization_id)
            await self._storage.forks.release_fork_retentions(target_session_id, target_thread_id=record.child_thread_id,
                                                            copy_operation_id=capture.copy_operation_id)
            sessions_root = self._storage.storage.sessions_dir
            async with NavigationTopologyGate(sessions_root).shared(), SessionLifecycleGate(sessions_root).exclusive(target_session_id):
                if self.source.attachment_store is not None:
                    self.source.attachment_store.abort_copy_attachments(capture, record)
                catalog.abort_unpublished_fork_capture(capture.copy_operation_id, reason="child publication failed",
                                                      child_creation_preimage_hash=record.preimage_hash)
            remove_captured_snapshot(self._storage.storage.sessions_dir, capture)
            return has_rollout

    def mark_published_fork(self, record: SessionCreationRecord) -> None:
        capture = self._storage.forks._catalog_store().get_fork_source_capture(creation_key=record.session_creation_idempotency_key)
        if capture is not None and self.source.attachment_store is not None:
            self.source.attachment_store.mark_copy_published(capture, publication_preimage_hash=record.preimage_hash)

    async def finalize_published_fork(self, record: SessionCreationRecord) -> None:
        capture = self._storage.forks._catalog_store().get_fork_source_capture(creation_key=record.session_creation_idempotency_key)
        if capture is not None and self.source.attachment_store is not None:
            await self.source.attachment_store.finalize_copy_attachments(capture, publication_preimage_hash=record.preimage_hash)

    def abort_prepared_capture(self, record: SessionCreationRecord, reason: str) -> None:
        catalog = self._storage.forks._catalog_store()
        capture = catalog.get_fork_source_capture(creation_key=record.session_creation_idempotency_key)
        if capture is None:
            return
        if self.source.attachment_store is not None:
            self.source.attachment_store.abort_copy_attachments(capture, record)
        if capture.state != "aborted":
            # 未发布 target 即使已完成 rollout 物化，也允许创建 operation 失败终结。
            catalog.abort_unpublished_fork_capture(capture.copy_operation_id, reason=reason)
        remove_captured_snapshot(self._storage.storage.sessions_dir, capture)
