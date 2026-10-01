"""ingest record 与 digest claim 这条垂直链路。

承载 attachment_ingest_records 的 create-or-get/推进/终止/查询，以及
attachment_blob_commit_claims 的 create-or-get（竞争唯一 digest claim）与
推进发布。宿主为 AttachmentBlobCatalog，只依赖宿主的 _connected() 与
write_transaction()。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
    BlobIdentityConflictError,
    blob_id_for_digest,
    date_bucket_relative_locator,
    utc_bucket_date,
    validate_blob_id,
    validate_digest,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    validate_ingest_staging_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentIngestRecord,
    BlobCommitClaim,
    _claim_from_row,
    _ingest_record_from_row,
)
from app.services.infrastructure.attachment_blob_catalog.schema import (
    INGEST_RECORD_TERMINAL_STATES,
    _validate_non_empty,
)


def _update_ingest_record_identity(
    connection: sqlite3.Connection,
    *,
    ingest_idempotency_key: str,
    identity: BlobIdentity,
    now_text: str,
    state: str,
) -> None:
    """推进 ingest record 的 digest/blob_id/length 与状态（状态机 UPDATE 单点）。

    mark_ingest_hashed（走 hashed）与 publish_blob_and_owner_ref（走
    published）共用同一条 UPDATE，避免状态迁移语句在两处漂移。
    """
    connection.execute(
        "UPDATE attachment_ingest_records SET state = ?, digest = ?, "
        "blob_id = ?, length = ?, updated_at = ? "
        "WHERE ingest_idempotency_key = ?",
        (
            state,
            identity.digest,
            identity.blob_id,
            identity.length,
            now_text,
            ingest_idempotency_key,
        ),
    )


class IngestMixin:
    """ingest record 与 digest claim 方法族（唯一实现点）。"""


    # ------------------------------------------------------------------
    # ingest record（preparing → hashed → published / aborted）
    # ------------------------------------------------------------------

    def create_or_get_ingest_record(
        self,
        *,
        ingest_idempotency_key: str,
        ingest_id: str,
        owner_session_id: str,
        owner_thread_id: str,
        pin_lease_id: str,
        pin_fencing_token: int,
        pin_captured_generation: int,
        preimage_hash: str,
        staging_relative_locator: str,
        max_bytes: int,
    ) -> AttachmentIngestRecord:
        """create-or-get ``state=preparing`` record；同 key 不同 preimage 冲突。"""
        _validate_non_empty("ingest_idempotency_key", ingest_idempotency_key)
        _validate_non_empty("pin_lease_id", pin_lease_id)
        _validate_non_empty("owner_session_id", owner_session_id)
        _validate_non_empty("owner_thread_id", owner_thread_id)
        validate_ingest_staging_relative_locator(staging_relative_locator)
        if staging_relative_locator != f".staging/{ingest_id}":
            raise ValueError(
                "staging locator 与 ingest_id 不一致（调用方不得拼路径）: "
                f"staging={staging_relative_locator!r}, ingest_id={ingest_id!r}"
            )
        for name, value in (
            ("pin_fencing_token", pin_fencing_token),
            ("pin_captured_generation", pin_captured_generation),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是 >= 0 的整数: {value!r}")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError(f"max_bytes 必须是正整数: {max_bytes!r}")
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE ingest_idempotency_key = ?",
                (ingest_idempotency_key,),
            ).fetchone()
            if existing is not None:
                record = _ingest_record_from_row(existing)
                if record.preimage_hash != preimage_hash:
                    raise RuntimeError(
                        "同 ingest idempotency key 的 preimage 冲突（fail closed）: "
                        f"key={ingest_idempotency_key!r}"
                    )
                return record
            connection.execute(
                "INSERT INTO attachment_ingest_records ("
                "ingest_idempotency_key, ingest_id, owner_session_id, "
                "owner_thread_id, pin_lease_id, pin_fencing_token, "
                "pin_captured_generation, preimage_hash, staging_relative_locator, "
                "max_bytes, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                (
                    ingest_idempotency_key,
                    ingest_id,
                    owner_session_id,
                    owner_thread_id,
                    pin_lease_id,
                    pin_fencing_token,
                    pin_captured_generation,
                    preimage_hash,
                    staging_relative_locator,
                    max_bytes,
                    now_text,
                    now_text,
                ),
            )
            inserted = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE ingest_idempotency_key = ?",
                (ingest_idempotency_key,),
            ).fetchone()
            return _ingest_record_from_row(inserted)

    def mark_ingest_hashed(
        self,
        *,
        ingest_idempotency_key: str,
        identity: BlobIdentity,
    ) -> tuple[AttachmentIngestRecord, BlobCommitClaim]:
        """事务内推进 record→``hashed`` 并 create-or-get 唯一 digest claim。

        claim 以 digest 唯一约束竞争：已有同 digest claim 时返回胜出 claim
        （record 绑定胜出 blob_id），**不生成第二 locator**；同 blob-id 不同
        length 视为 identity conflict。
        """
        validate_digest(identity.digest)
        validate_blob_id(identity.blob_id)
        if identity.blob_id != blob_id_for_digest(identity.digest):
            raise ValueError(
                "blob_id 与 digest 不匹配（身份三元组必须逐字节一致）: "
                f"blob_id={identity.blob_id!r}, digest={identity.digest!r}"
            )
        # 日期分桶的日期来源必须是显式时间戳（这里取 claim 时刻的显式 UTC
        # datetime），绝不用文件 mtime 或进程本地时区。
        claim_date = utc_bucket_date(datetime.now(UTC))
        relative_locator = date_bucket_relative_locator(identity.blob_id, claim_date)
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            record = self._require_ingest_row(connection, ingest_idempotency_key)
            if record.state in INGEST_RECORD_TERMINAL_STATES:
                raise RuntimeError(
                    "ingest record 已终态，拒绝重新推进 hashed: "
                    f"key={ingest_idempotency_key!r}, state={record.state!r}"
                )
            if record.state == "preparing" and identity.length > record.max_bytes:
                raise ValueError(
                    "附件超过 record 冻结的大小限制: "
                    f"length={identity.length}, max_bytes={record.max_bytes}"
                )
            claim_row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
                (identity.digest,),
            ).fetchone()
            if claim_row is None:
                connection.execute(
                    "INSERT INTO attachment_blob_commit_claims ("
                    "digest, blob_id, final_relative_locator, expected_length, "
                    "first_claim_utc_date, winning_ingest_id, state, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)",
                    (
                        identity.digest,
                        identity.blob_id,
                        relative_locator,
                        identity.length,
                        claim_date.isoformat(),
                        record.ingest_id,
                        now_text,
                        now_text,
                    ),
                )
            else:
                claim = _claim_from_row(claim_row)
                if (
                    claim.blob_id != identity.blob_id
                    or claim.expected_length != identity.length
                ):
                    raise BlobIdentityConflictError(
                        "同一 digest 的 claim 与本次身份不一致（blob-identity-conflict）: "
                        f"digest={identity.digest!r}, "
                        f"claimed_blob_id={claim.blob_id!r}, "
                        f"actual_blob_id={identity.blob_id!r}, "
                        f"claimed_length={claim.expected_length}, "
                        f"actual_length={identity.length}"
                    )
            _update_ingest_record_identity(
                connection,
                ingest_idempotency_key=ingest_idempotency_key,
                identity=identity,
                now_text=now_text,
                state="hashed",
            )
            updated = self._require_ingest_row(connection, ingest_idempotency_key)
            claim_row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
                (identity.digest,),
            ).fetchone()
            return updated, _claim_from_row(claim_row)

    def abort_ingest_record(
        self, *, ingest_idempotency_key: str, reason: str
    ) -> AttachmentIngestRecord:
        """把非终态 record 收敛为 ``aborted``（失败清理/删除 drain）。"""
        _validate_non_empty("reason", reason)
        with self.write_transaction() as connection:
            record = self._require_ingest_row(connection, ingest_idempotency_key)
            if record.state in INGEST_RECORD_TERMINAL_STATES:
                return record
            connection.execute(
                "UPDATE attachment_ingest_records SET state = 'aborted', "
                "abort_reason = ?, updated_at = ? "
                "WHERE ingest_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), ingest_idempotency_key),
            )
            return self._require_ingest_row(connection, ingest_idempotency_key)

    def get_ingest_record(self, ingest_idempotency_key: str) -> AttachmentIngestRecord:
        """按 key 读取 record；缺失抛 KeyError。"""
        connection = self._connected()
        return _ingest_record_from_row(
            self._require_ingest_row(connection, ingest_idempotency_key)
        )

    def list_non_terminal_ingest_records(
        self, *, owner_session_id: str | None = None
    ) -> tuple[AttachmentIngestRecord, ...]:
        """按状态索引枚举非终态 record（恢复/删除 drain 只读持久 record）。"""
        connection = self._connected()
        if owner_session_id is None:
            rows = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE state NOT IN ('published', 'aborted') "
                "ORDER BY created_at, rowid"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE owner_session_id = ? "
                "AND state NOT IN ('published', 'aborted') "
                "ORDER BY created_at, rowid",
                (owner_session_id,),
            ).fetchall()
        return tuple(_ingest_record_from_row(row) for row in rows)

    # ------------------------------------------------------------------
    # blob commit claim
    # ------------------------------------------------------------------

    def get_claim_by_digest(self, digest: str) -> BlobCommitClaim | None:
        """按 digest 取唯一 claim；无 claim 返回 None。"""
        validate_digest(digest)
        row = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
            (digest,),
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    def get_claim_by_blob_id(self, blob_id: str) -> BlobCommitClaim | None:
        """按 blob-id 取 claim；无 claim 返回 None。"""
        validate_blob_id(blob_id)
        row = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
            (blob_id,),
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    def list_non_terminal_claims(self) -> tuple[BlobCommitClaim, ...]:
        """按状态索引枚举非终态 claim（rename 后未发布崩溃的定点恢复）。"""
        rows = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims "
            "WHERE state NOT IN ('published', 'aborted') "
            "ORDER BY created_at, rowid"
        ).fetchall()
        return tuple(_claim_from_row(row) for row in rows)

    def mark_claim_published(self, *, blob_id: str) -> BlobCommitClaim:
        """把 claim 推进为 ``published``（availability + owner ref 同事务已提交）。"""
        validate_blob_id(blob_id)
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
                (blob_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"blob commit claim 不存在: blob_id={blob_id!r}, "
                    f"path={self.database_path}"
                )
            claim = _claim_from_row(row)
            if claim.state == "published":
                return claim
            if claim.state == "aborted":
                raise RuntimeError(
                    f"claim 已 aborted，拒绝发布: blob_id={blob_id!r}"
                )
            connection.execute(
                "UPDATE attachment_blob_commit_claims SET state = 'published', "
                "updated_at = ? WHERE blob_id = ?",
                (datetime.now(UTC).isoformat(), blob_id),
            )
            updated = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
                (blob_id,),
            ).fetchone()
            return _claim_from_row(updated)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_ingest_row(
        self, connection: sqlite3.Connection, ingest_idempotency_key: str
    ) -> AttachmentIngestRecord:
        row = connection.execute(
            "SELECT * FROM attachment_ingest_records "
            "WHERE ingest_idempotency_key = ?",
            (ingest_idempotency_key,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"attachment ingest record 不存在: "
                f"key={ingest_idempotency_key!r}, path={self.database_path}"
            )
        return _ingest_record_from_row(row)
