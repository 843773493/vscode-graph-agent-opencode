"""copy 附件 settlement 与 lease 共用 SessionControlStore 的连接和事务。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.sqlite_state import load_sql
from app.domain.itemized.hashing import canonical_json_bytes

if TYPE_CHECKING:
    from app.core.session_control_store import SessionControlStore

COPY_ATTACHMENT_SETTLEMENT_DDL = load_sql(__package__, "sql/copy_attachment_settlements.sql")


@dataclass(frozen=True, slots=True)
class CopyAttachmentSettlementRecord:
    copy_operation_id: str
    preimage_hash: str
    target_session_id: str
    target_thread_id: str
    captured_generation: int
    publication_preimage_hash: str
    claim_ids_json: str
    claim_manifest_sha256: str
    lease_id: str
    lease_fencing_token: int
    state: str

    @property
    def claim_ids(self) -> tuple[str, ...]:
        ids = json.loads(self.claim_ids_json)
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids) or sorted(set(ids)) != ids or (
            hashlib.sha256(canonical_json_bytes(ids)).hexdigest() != self.claim_manifest_sha256
        ):
            raise RuntimeError("copy attachment settlement claim manifest 已损坏")
        return tuple(ids)


def read_copy_attachment_settlements(control: SessionControlStore) -> tuple[CopyAttachmentSettlementRecord, ...]:
    control._ensure_open()
    return tuple(CopyAttachmentSettlementRecord(**dict(row)) for row in control.connection.execute(
        "SELECT * FROM copy_attachment_settlements ORDER BY copy_operation_id",
    ).fetchall())


def prepare_copy_attachment_settlement(
    control: SessionControlStore, *, copy_operation_id: str, preimage_hash: str,
    target_session_id: str, target_thread_id: str, publication_preimage_hash: str,
    claim_ids: tuple[str, ...],
) -> CopyAttachmentSettlementRecord:
    if not claim_ids or tuple(sorted(set(claim_ids))) != claim_ids:
        raise ValueError("copy attachment settlement 必须绑定有序非空 claim 集合")
    claim_json = canonical_json_bytes(list(claim_ids)).decode()
    claim_hash = hashlib.sha256(claim_json.encode()).hexdigest()
    with control._write_transaction() as connection:
        row = connection.execute("SELECT * FROM copy_attachment_settlements WHERE copy_operation_id = ?", (copy_operation_id,)).fetchone()
        if row is not None:
            if tuple(row[field] for field in (
                "preimage_hash", "target_session_id", "target_thread_id", "publication_preimage_hash", "claim_ids_json", "claim_manifest_sha256",
            )) != (preimage_hash, target_session_id, target_thread_id, publication_preimage_hash, claim_json, claim_hash) or row["state"] in {"aborted", "target_deleted"}:
                raise RuntimeError("copy attachment settlement 冻结前像冲突或已删除")
            return CopyAttachmentSettlementRecord(**dict(row))
        pin = control.create_or_get_lease(
            operation_kind="attachment", operation_identity="copy-attachment:" + copy_operation_id,
            preimage_hash=preimage_hash, recovery_ref=copy_operation_id, transaction_connection=connection,
        )
        connection.execute(
            "INSERT INTO copy_attachment_settlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing')",
            (copy_operation_id, preimage_hash, target_session_id, target_thread_id,
             pin.captured_lifecycle_generation, publication_preimage_hash, claim_json, claim_hash,
             pin.lease_id, pin.fencing_token),
        )
        return CopyAttachmentSettlementRecord(**dict(connection.execute("SELECT * FROM copy_attachment_settlements WHERE copy_operation_id = ?", (copy_operation_id,)).fetchone()))


def transition_copy_attachment_settlement(
    control: SessionControlStore, record: CopyAttachmentSettlementRecord, *, state: str,
) -> CopyAttachmentSettlementRecord:
    """调用者先收敛全部 claim/ref，本事务再收敛 record 与原 lease owner。"""
    transitions = {
        "preparing": {"published_pending_attachment_commit", "aborted", "target_deleted"},
        "published_pending_attachment_commit": {"committed", "target_deleted"},
        "committed": {"target_deleted"}, "aborted": set(), "target_deleted": set(),
    }
    with control._write_transaction() as connection:
        row = connection.execute("SELECT * FROM copy_attachment_settlements WHERE copy_operation_id = ?", (record.copy_operation_id,)).fetchone()
        if row is None:
            raise KeyError(record.copy_operation_id)
        current = CopyAttachmentSettlementRecord(**dict(row))
        if current.state == state:
            return current
        if current != record or state not in transitions[current.state]:
            raise RuntimeError("copy attachment settlement 终态/前像 CAS 冲突")
        connection.execute("UPDATE copy_attachment_settlements SET state = ? WHERE copy_operation_id = ? AND state = ?", (state, record.copy_operation_id, record.state))
        if state in {"committed", "aborted", "target_deleted"}:
            pin = control.get_lease(record.lease_id)
            if pin.fencing_token != record.lease_fencing_token:
                raise RuntimeError("copy attachment settlement lease fencing 已变化")
            if pin.state == "active":
                pin = control.mark_lease_settling(lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token, transaction_connection=connection)
            if pin.state == "settling":
                control.settle_lease(lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token,
                                     outcome="completed" if state == "committed" else "cancelled", transaction_connection=connection)
        return CopyAttachmentSettlementRecord(**dict(connection.execute("SELECT * FROM copy_attachment_settlements WHERE copy_operation_id = ?", (record.copy_operation_id,)).fetchone()))
