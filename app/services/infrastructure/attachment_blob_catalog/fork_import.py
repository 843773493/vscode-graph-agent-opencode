"""附件跨域导入及终态 staging 的唯一生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.abstractions.session_context import SessionForkTransportProtocol
from app.core.phase_observer import observe_phase
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_lifecycle_gate import OperationHolderGate
from app.schemas.internal_v2.session import SessionForkRequest
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    fork_import_ingest_id,
    ingest_staging_relative_locator,
    resolve_blob_path,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.rollout_context.fork.transfer import (
    receive_transfer_file,
)


class ForkAttachmentImportMixin:
    """导入 holder 覆盖全部 staging 写入；终态清理只非阻塞确认旧 writer 已停止。"""

    _catalog: AttachmentBlobCatalog
    _attachments_root: Path
    _fork_imports: OperationHolderGate
    _blob_commits: OperationHolderGate
    _parse_logical_file_id: Callable[[str, str], str]
    _verify_blob_content: Callable[[Path, str, int, str], None]
    _commit_staged_blob: Callable[..., None]

    async def accept_fork_import(
        self, capture: ForkSourceCaptureRecord, snapshots: tuple[ForkAttachmentSnapshot, ...],
        *, transport: SessionForkTransportProtocol, request: SessionForkRequest,
    ) -> None:
        """来源 receipt 与冻结 snapshot 已完整校验；只为本 target 创建 reserved claim。"""
        async with self._fork_imports.lock_for(capture.copy_operation_id):
            if any((snapshot.target_session_id, snapshot.target_thread_id) != (capture.target_session_id, capture.target_thread_id)
                   for snapshot in snapshots):
                raise RuntimeError("fork imported 附件 snapshot 不属于本 target owner")
            for snapshot in snapshots:
                if self._parse_logical_file_id(capture.target_session_id, snapshot.target_file_id) != snapshot.target_attachment_id:
                    raise RuntimeError("fork imported 附件 URI 与冻结 target identity 不一致")
            self._catalog.prepare_fork_attachment_claims(
                copy_operation_id=capture.copy_operation_id, source_snapshot_id=capture.snapshot_id,
                operation_preimage_hash=capture.preimage_hash, snapshots=snapshots,
                imported_locators={snapshot.blob_id: None for snapshot in snapshots if snapshot.availability == "available"},
            )
            for snapshot in snapshots:
                if snapshot.availability != "available":
                    continue
                ingest_id, locator = self._catalog.reserve_fork_blob_import(capture.copy_operation_id, snapshot)
                final_path = resolve_blob_path(self._attachments_root, locator)
                async with self._blob_commits.lock_for(snapshot.blob_id):
                    if final_path.is_symlink():
                        raise RuntimeError("fork imported 附件正文不得是符号链接")
                    if final_path.is_file():
                        self._verify_blob_content(final_path, snapshot.digest, snapshot.length, snapshot.blob_id)
                        self._catalog.prepare_fork_attachment_claims(
                            copy_operation_id=capture.copy_operation_id, source_snapshot_id=capture.snapshot_id,
                            operation_preimage_hash=capture.preimage_hash, snapshots=(snapshot,),
                            imported_locators={snapshot.blob_id: locator},
                        )
                        continue
                staging = resolve_staging_path(self._attachments_root, ingest_staging_relative_locator(ingest_id))
                if staging.is_symlink():
                    raise RuntimeError("fork imported 附件 staging 不得是符号链接")
                staging.unlink(missing_ok=True)
                try:
                    # 网络接收只受原 fork claim 保活，不占正文提交分片或 Session gate。
                    await receive_transfer_file(
                        transport, request, capture, path=staging, size_bytes=snapshot.length,
                        action="attachment_chunk", blob_id=snapshot.blob_id,
                    )
                    self._verify_blob_content(staging, snapshot.digest, snapshot.length, snapshot.blob_id)
                    await observe_phase(capture.copy_operation_id, "copy.attachments.import_staged", details={"blob_id": snapshot.blob_id, "ingest_id": ingest_id})
                    async with self._blob_commits.lock_for(snapshot.blob_id):
                        self._commit_staged_blob(staging_path=staging, final_path=final_path,
                                                 identity=BlobIdentity(snapshot.blob_id, snapshot.digest, snapshot.length))
                        self._catalog.prepare_fork_attachment_claims(
                            copy_operation_id=capture.copy_operation_id, source_snapshot_id=capture.snapshot_id,
                            operation_preimage_hash=capture.preimage_hash, snapshots=(snapshot,),
                            imported_locators={snapshot.blob_id: locator},
                        )
                finally:
                    staging.unlink(missing_ok=True)

    async def collect_fork_import_staging(self, copy_operation_id: str | None = None) -> tuple[str, ...]:
        """只消费终态 claim 的冻结快照，不扫盘，也不等待仍在接收网络数据的 writer。"""
        operations = ((copy_operation_id,) if copy_operation_id is not None
                      else self._catalog.terminal_fork_attachment_operations())
        removed = []
        for operation in operations:
            async with self._fork_imports.lock_for(operation, wait=False) as holder:
                if not holder.acquired:
                    continue
                # holder 内重查；settlement 终态本身不能证明旧 writer 已退出。
                for snapshot in self._catalog.terminal_fork_attachment_snapshots(operation):
                    if snapshot.target_action != "copy_snapshot" or snapshot.availability != "available":
                        continue
                    ingest_id = fork_import_ingest_id(operation, snapshot.blob_id)
                    path = resolve_staging_path(self._attachments_root, ingest_staging_relative_locator(ingest_id))
                    if path.is_symlink():
                        raise RuntimeError("fork imported 附件 staging 不得是符号链接")
                    if path.exists():
                        path.unlink()
                        removed.append(ingest_id)
        return tuple(removed)
