"""附件上传 intent、独立目录发布与 pin 对账；不以 digest 合并资源。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.identifier import create_uuid_hex
from app.core.session_lifecycle_gate import SessionOperationLease
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    ingest_staging_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentFileVersion,
    DirectoryIngestRecord,
)


class IngestMixin:
    def get_ingest_record(self, ingest_idempotency_key: str) -> DirectoryIngestRecord:
        row = self._connected().execute(
            "SELECT * FROM attachment_directory_ingests WHERE ingest_idempotency_key=?", (ingest_idempotency_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"附件上传 intent 不存在：operation={ingest_idempotency_key}")
        return DirectoryIngestRecord(**dict(row))

    def create_ingest(
        self, *, operation_id: str, ingest_id: str, session_id: str, thread_id: str,
        message_id: str, pin: SessionOperationLease, digest: str, length: int,
        file_name: str | None, mime_type: str, now: datetime,
    ) -> DirectoryIngestRecord:
        """先冻结稳定身份和日期；相同字节的其它上传得到另一目录。"""
        with self.write_transaction() as connection:
            row = connection.execute("SELECT * FROM attachment_directory_ingests WHERE ingest_idempotency_key=?", (operation_id,)).fetchone()
            if row is not None:
                previous = DirectoryIngestRecord(**dict(row))
                if (previous.ingest_id, previous.owner_session_id, previous.owner_thread_id, previous.message_id,
                    previous.pin_lease_id, previous.pin_captured_generation, previous.preimage_hash,
                    previous.digest, previous.length, previous.file_name, previous.mime_type) != (
                    ingest_id, session_id, thread_id, message_id, pin.lease_id, pin.captured_lifecycle_generation,
                    pin.preimage_hash, digest, length, file_name, mime_type,
                ):
                    raise RuntimeError("相同附件上传操作的冻结输入冲突")
                if previous.state == "aborted":
                    raise RuntimeError("附件上传操作已终止，不能重开原 intent")
                return previous
            attachment_id = "att_" + create_uuid_hex(namespace="attachment_directory")
            timestamp = now.astimezone(UTC).isoformat()
            connection.execute(
                "INSERT INTO attachment_directory_ingests ("
                "ingest_idempotency_key,ingest_id,attachment_id,owner_session_id,owner_thread_id,message_id,"
                "pin_lease_id,pin_fencing_token,pin_captured_generation,preimage_hash,staging_relative_locator,relative_locator,"
                "digest,length,file_name,mime_type,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'preparing',?,?)",
                (operation_id, ingest_id, attachment_id, session_id, thread_id, message_id, pin.lease_id,
                 pin.fencing_token, pin.captured_lifecycle_generation, pin.preimage_hash,
                 ingest_staging_relative_locator(ingest_id), attachment_directory_relative_locator(attachment_id, now),
                 digest, length, file_name, mime_type, timestamp, timestamp),
            )
            return self.get_ingest_record(operation_id)

    def mark_ingest_durable(self, operation_id: str, *, now: datetime) -> DirectoryIngestRecord:
        """正文目录 durability 完成后才能进入发布；真实字节由原 store 核验。"""
        with self.write_transaction() as connection:
            record = self.get_ingest_record(operation_id)
            if record.state == "aborted":
                raise RuntimeError("不能发布已终止的附件上传")
            if record.state == "preparing":
                connection.execute("UPDATE attachment_directory_ingests SET state='durable',updated_at=? WHERE ingest_idempotency_key=?", (now.astimezone(UTC).isoformat(), operation_id))
            return self.get_ingest_record(operation_id)

    def publish_ingest(self, operation_id: str, *, now: datetime) -> DirectoryIngestRecord:
        """同一 SQLite 事务发布目录、原件版本、精确关联和 ingest 终态。"""
        with self.write_transaction() as connection:
            record = self.get_ingest_record(operation_id)
            if record.state == "published":
                self.require_available_directory(record.attachment_id)
                if self.file_version(record.attachment_id, "original/content") != AttachmentFileVersion(
                    record.attachment_id, "original/content", "original", record.digest,
                    record.length, record.file_name, record.mime_type,
                ):
                    raise RuntimeError("已发布上传缺少原冻结原件版本")
                return record
            if record.state != "durable":
                raise RuntimeError("附件目录尚未 durable，拒绝发布")
            timestamp = now.astimezone(UTC).isoformat()
            connection.execute(
                "INSERT INTO attachment_directories VALUES (?,?, 'available','private',?,?,NULL,NULL,NULL)",
                (record.attachment_id, record.relative_locator, record.created_at, timestamp),
            )
            self._insert_file_version(connection, AttachmentFileVersion(
                record.attachment_id, "original/content", "original", record.digest,
                record.length, record.file_name, record.mime_type,
            ))
            self._attach(connection, record.attachment_id, record.owner_session_id,
                         record.owner_thread_id, "upload:" + operation_id, now)
            connection.execute("UPDATE attachment_directory_ingests SET state='published',updated_at=? WHERE ingest_idempotency_key=?", (timestamp, operation_id))
            return self.get_ingest_record(operation_id)

    def abort_ingest_record(self, *, ingest_idempotency_key: str, reason: str, now: datetime) -> None:
        with self.write_transaction() as connection:
            record = self.get_ingest_record(ingest_idempotency_key)
            if record.state == "published":
                raise RuntimeError("不能把已发布的附件上传倒写为 aborted")
            if record.state != "aborted":
                connection.execute(
                    "UPDATE attachment_directory_ingests SET state='aborted',abort_reason=?,updated_at=? WHERE ingest_idempotency_key=?",
                    (reason, now.astimezone(UTC).isoformat(), ingest_idempotency_key),
                )

    def list_non_terminal_ingest_records(self, *, owner_session_id: str) -> tuple[DirectoryIngestRecord, ...]:
        return tuple(DirectoryIngestRecord(**dict(row)) for row in self._connected().execute(
            "SELECT * FROM attachment_directory_ingests WHERE owner_session_id=? AND state IN ('preparing','durable') ORDER BY ingest_idempotency_key",
            (owner_session_id,),
        ))

    def release_unused_ingest(self, record: DirectoryIngestRecord, *, now: datetime) -> bool:
        """只释放该上传的原关联；fork 或新代关联不能被迟到恢复解除。"""
        with self.write_transaction() as connection:
            changed = connection.execute(
                "UPDATE attachment_directory_associations SET state='released',updated_at=? "
                "WHERE attachment_id=? AND session_id=? AND thread_id=? AND binding_token=? AND state='active'",
                (now.astimezone(UTC).isoformat(), record.attachment_id, record.owner_session_id,
                 record.owner_thread_id, "upload:" + record.ingest_idempotency_key),
            ).rowcount
            self._mark_zero_associations(connection, record.attachment_id, now)
            return bool(changed)
