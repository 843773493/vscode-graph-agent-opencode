"""目录快照只从原 capture 文件树导入；原 holder 覆盖真实复制 writer 的全部生命周期。"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime

from app.core.atomic_fs import fsync_directory
from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_resource_uri
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.core.trace_middleware import get_current_gateway_id
from app.services.infrastructure.attachment_blob_catalog.directories import (
    copy_directory_files,
    directory_manifest,
    protect_original,
    remove_directory,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    ingest_staging_relative_locator,
    resolve_attachment_directory,
    resolve_captured_directory,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentDirectorySnapshot,
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
)


def _import_id(operation_id: str, attachment_id: str) -> str:
    return "ing_" + hashlib.sha256(f"directory-fork:{operation_id}:{attachment_id}".encode()).hexdigest()[:32]


class ForkAttachmentImportMixin:
    async def accept_fork_import(self, capture: ForkSourceCaptureRecord, snapshots: tuple[ForkAttachmentSnapshot | ForkAttachmentDirectorySnapshot, ...]) -> None:
        try:
            async with self._fork_imports.lock_for(capture.copy_operation_id):
                for snapshot in snapshots:
                    if not isinstance(snapshot, ForkAttachmentDirectorySnapshot):
                        raise TypeError("旧 blob fork 必须在升级前完成或撤销；目录 owner 不创建旧格式 import")
                    if (snapshot.target_session_id, snapshot.target_thread_id) != (capture.target_session_id, capture.target_thread_id):
                        raise RuntimeError("fork 目录快照不属于精确目标 Thread")
                    if snapshot.availability != "available":
                        continue
                    parsed = parse_resource_uri(snapshot.target_file_id, local_gateway_id=get_current_gateway_id())
                    if (parsed.kind, parsed.scope, parsed.scope_id, parsed.gateway_authority, parsed.attachment_id) != (
                        "attachments", "workspace", self.workspace_id, None, snapshot.target_attachment_id,
                    ) or len(parsed.tail_segments) < 2 or snapshot.target_action != "copy_snapshot":
                        raise RuntimeError("fork 目录 import 的 URI 或目标动作不属于本 owner")
                self._catalog.prepare_fork_attachment_claims(copy_operation_id=capture.copy_operation_id, source_snapshot_id=capture.snapshot_id,
                                                           operation_preimage_hash=capture.preimage_hash, snapshots=snapshots)
                for snapshot in snapshots:
                    if snapshot.availability != "available":
                        continue
                    async with self._directory_operations.lock_for(snapshot.target_attachment_id):
                        if not await run_sync_shielded(self._import_directory, capture, snapshot):
                            raise RuntimeError("目录 fork 在复制期间已终止，拒绝发布")
                    await observe_phase(capture.copy_operation_id, "copy.attachments.import_staged", details={"attachment_id": snapshot.target_attachment_id})
        finally:
            # writer 排空并释放原 holder 后才清终态残留；取消继续向外传播。
            await run_cleanup_shielded(lambda: self.collect_fork_import_staging(capture.copy_operation_id))

    def _import_directory(self, capture: ForkSourceCaptureRecord, snapshot: ForkAttachmentDirectorySnapshot) -> bool:
        claim = self._catalog.connection.execute("SELECT state FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
        if claim is None or claim[0] in {"aborted", "target_deleted"}:
            raise RuntimeError("目录 import 缺少有效原 claim")
        if claim[0] == "committed":
            # 已发布目标的派生文件可以继续变化；重试不能覆盖或倒写过去的目录版本。
            return True
        locator = attachment_directory_relative_locator(snapshot.target_attachment_id, datetime.fromisoformat(snapshot.target_created_at))
        destination = resolve_attachment_directory(self._attachments_root, locator)
        staging = resolve_staging_path(self._attachments_root, ingest_staging_relative_locator(_import_id(capture.copy_operation_id, snapshot.target_attachment_id)))
        if destination.exists():
            if directory_manifest(destination) != (snapshot.files, snapshot.directories):
                raise RuntimeError("已存在的目录 import 与原冻结版本不一致，拒绝覆盖")
        else:
            capture_root = capture_directory(self._sessions_root, capture) / capture.source_session_id
            source = resolve_captured_directory(capture_root, snapshot.source_attachment_id)
            copy_directory_files(source, staging, snapshot.files, snapshot.directories)
            destination.parent.mkdir(parents=True, exist_ok=True)
        # 复制不占写事务；最终 claim 复核、短 rename 和 catalog 发布与中止串行。
        with self._catalog.write_transaction() as connection:
            claim = connection.execute("SELECT state FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
            if claim is not None and claim[0] in {"aborted", "target_deleted"}:
                return False
            self._catalog.publish_fork_directory(connection, snapshot)
            if not destination.exists():
                os.rename(staging, destination)
                fsync_directory(destination.parent)
                fsync_directory(staging.parent)
            protect_original(destination)
        return True

    async def collect_fork_import_staging(self, copy_operation_id: str | None = None) -> tuple[str, ...]:
        operations = (copy_operation_id,) if copy_operation_id is not None else self._catalog.terminal_fork_attachment_operations()
        removed = []
        for operation in operations:
            async with self._fork_imports.lock_for(operation, wait=False) as holder:
                if not holder.acquired:
                    continue
                for snapshot in self._catalog.terminal_fork_attachment_snapshots(operation):
                    staging = resolve_staging_path(self._attachments_root, ingest_staging_relative_locator(_import_id(operation, snapshot.target_attachment_id)))
                    if staging.exists():
                        await run_sync_shielded(remove_directory, staging)
                        removed.append(snapshot.target_attachment_id)
        return tuple(removed)
