"""浏览器预上传复用原 ingest/pin；未发送附件按明确期限收敛。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from tempfile import TemporaryFile
from typing import TYPE_CHECKING

from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_file_version,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    DirectoryIngestRecord,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)

if TYPE_CHECKING:
    from app.services.infrastructure.attachment_blob_catalog.recovery import (
        AttachmentMessageReader,
    )

# 只约束未发送的预上传，不按时间删除消息附件及其可靠版本。
DRAFT_UPLOAD_RETENTION = timedelta(hours=24)
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024


def is_draft_upload(message_id: str) -> bool:
    return message_id.startswith("draft_")


class AttachmentUploadsMixin:
    async def upload_attachment(self, session_id: str, thread_id: str, *, name: str,
                                content_type: str, chunks: AsyncIterator[bytes]) -> AttachmentRef:
        """传输中的临时文件由 OS 句柄托管；完整接收后才发布可引用附件。"""
        self._catalog.acquire_access()
        async with self.deletion_holder.lock_for(session_id, exclusive=False):
            async with self._topology_gate.shared(), self._gate.exclusive(session_id):
                self._assert_session_active(session_id, thread_id)
            await run_sync_shielded(self._attachments_root.mkdir, parents=True, exist_ok=True)
            # TemporaryFile 在 Unix 创建后即 unlink，Windows 使用 delete-on-close；
            # 断连/取消关闭句柄，硬退出也不会留下需扫盘猜测归属的半文件。
            with TemporaryFile(dir=self._attachments_root) as temporary:
                length = 0
                async for chunk in chunks:
                    length += len(chunk)
                    if length > MAX_ATTACHMENT_BYTES:
                        raise ValueError("附件超过 30 MiB 限制")
                    await run_sync_shielded(temporary.write, chunk)
                await run_sync_shielded(temporary.seek, 0)
                data = await run_sync_shielded(temporary.read)
            draft_id = "draft_" + create_uuid_hex(namespace="attachment_upload")
            return await self._persist_content(session_id, thread_id, draft_id, 0,
                AttachmentRef(file_id=draft_id, name=name, content_type=content_type), data, content_type)

    def _discard_draft_record(self, record: DirectoryIngestRecord) -> bool:
        """调用方已排空 Session 上传；只回收该次未发送上传，不动消息资产。"""
        self._catalog.release_unused_ingest(record, now=self._clock())
        if self._leases is None:
            raise RuntimeError("附件回收缺少资源占用账本")
        with self.directory_access(record.attachment_id):
            directory = self._catalog.get_directory(record.attachment_id)
            if directory is None:
                return False
            if directory.availability != "tombstoned":
                self._release_directory(record.attachment_id, self._leases)
                directory = self._catalog.get_directory(record.attachment_id)
                if directory.availability != "released":
                    return False
                # 工作副本已经按基线核实；墓碑保留审计身份，不再允许重建未发送附件。
                self._catalog.require_releasable_directory(record.attachment_id, now=self._clock())
                with self._catalog.write_transaction() as connection:
                    connection.execute("UPDATE attachment_directories SET availability='tombstoned',release_pending=0 WHERE attachment_id=?",
                                       (record.attachment_id,))
            digests = tuple(row[0] for row in self._catalog.connection.execute(
                "SELECT digest FROM attachment_directory_files WHERE attachment_id=?", (record.attachment_id,),
            ))
            self._remove_unused_upload_versions(digests)
        return True

    def _remove_unused_upload_versions(self, digests: tuple[str, ...]) -> None:
        # 只核实本次废弃上传已登记的版本，不扫描磁盘；同字节其它附件仍可重建。
        with workspace_file_mutation_lock(self._workspace_root):
            connection = self._catalog.connection
            for digest in digests:
                in_use = connection.execute(
                    "SELECT 1 FROM attachment_directory_files f JOIN attachment_directories d USING(attachment_id) "
                    "WHERE f.digest=? AND d.availability<>'tombstoned' UNION ALL "
                    "SELECT 1 FROM attachment_directory_ingests WHERE digest=? AND state IN ('preparing','durable') LIMIT 1",
                    (digest, digest),
                ).fetchone()
                if in_use is None:
                    version = resolve_file_version(self._attachments_root, digest)
                    for path in (version, version.with_suffix(".partial")):
                        if path.exists():
                            path.chmod(0o600)
                            path.unlink()

    async def discard_upload(self, session_id: str, thread_id: str, file_id: str, *, messages: AttachmentMessageReader) -> None:
        """删除通知可迟到；已被任何消息使用的附件不随草稿删除。"""
        def discard() -> None:
            self._assert_session_active(session_id, thread_id)
            attachment_id, _ = self._resource_id(file_id, session_id=session_id, thread_id=thread_id)
            row = self._catalog.connection.execute(
                "SELECT ingest_idempotency_key FROM attachment_directory_ingests WHERE attachment_id=? "
                "AND owner_session_id=? AND owner_thread_id=? AND substr(message_id,1,6)='draft_'",
                (attachment_id, session_id, thread_id),
            ).fetchone()
            if row is None:
                raise ValueError("只能移除当前 Thread 尚未发送的预上传附件")
            record = self._catalog.get_ingest_record(row[0])
            if messages.attachment_reference_in_use(session_id, thread_id, attachment_id):
                return
            self._recover_ingests(session_id, messages=messages)
            control = self._open_control_store(session_id)
            try:
                pin = control.get_lease(record.pin_lease_id)
                if pin.state == "completed":
                    return
                self._discard_draft_record(record)
                if pin.state not in {"completed", "failed", "cancelled"}:
                    self._settle_pin(control, pin, outcome="failed")
            finally:
                control.close()

        async with self.deletion_holder.lock_for(session_id), self._gate.exclusive(session_id):
            await run_sync_shielded(discard)

    async def expire_draft_uploads(self, *, messages: AttachmentMessageReader) -> int:
        cutoff = (self._clock().astimezone(UTC) - DRAFT_UPLOAD_RETENTION).isoformat()

        def owners() -> tuple[str, ...]:
            return tuple(row[0] for row in self._catalog.connection.execute(
                "SELECT DISTINCT i.owner_session_id FROM attachment_directory_ingests i "
                "JOIN attachment_directory_associations a USING(attachment_id) WHERE substr(i.message_id,1,6)='draft_' "
                "AND i.created_at<=? AND a.state='active' "
                "AND (a.binding_token='upload:'||i.ingest_idempotency_key OR a.binding_token LIKE 'reference:%')", (cutoff,),
            ))

        settled = 0
        for session_id in await run_sync_shielded(owners):
            settled += (await self.recover_idle_session(session_id, messages=messages)).count("settled-pin")
        return settled
