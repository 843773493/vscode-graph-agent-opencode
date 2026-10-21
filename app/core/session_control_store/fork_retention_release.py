"""target 本地 fork retention 释放 journal；source claim 仍归原 catalog owner。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.core.session_catalog_store.contracts import (
    ForkRetentionClaim,
    ForkSourceCaptureRecord,
)
from app.core.sqlite_state import load_sql
from app.domain.itemized.hashing import canonical_json_bytes

if TYPE_CHECKING:
    import sqlite3

    from app.core.session_control_store import SessionControlStore
    from app.core.session_lifecycle_gate import SessionOperationLease


FORK_RETENTION_RELEASE_DDL = load_sql(__package__, "sql/fork_retention_releases.sql")


@dataclass(frozen=True, slots=True)
class ForkRetentionReleaseRecord:
    fork_retention_claim_id: str
    workspace_id: str
    source_session_id: str
    source_thread_id: str
    target_session_id: str
    target_thread_id: str
    source_lifecycle_generation: int
    copy_operation_id: str
    capture_preimage_hash: str
    checkpoint_ns: str
    release_reason: str
    preimage_hash: str
    state: str

    def verify_claim(self, claim: ForkRetentionClaim) -> None:
        if (
            claim.fork_retention_claim_id, claim.workspace_id,
            claim.source_session_id, claim.target_session_id,
            claim.source_lifecycle_generation,
        ) != (
            self.fork_retention_claim_id, self.workspace_id,
            self.source_session_id, self.target_session_id,
            self.source_lifecycle_generation,
        ):
            raise RuntimeError("fork retention release 与冻结 claim 前像不一致")

    def verify_capture(self, capture: ForkSourceCaptureRecord) -> None:
        request = json.loads(capture.request_json)
        if (
            capture.fork_id, capture.copy_operation_id, capture.preimage_hash,
            capture.source_session_id, capture.source_thread_id,
            capture.target_session_id, capture.target_thread_id,
            request["checkpoint_ns"], request["relationship"],
        ) != (
            self.fork_retention_claim_id, self.copy_operation_id, self.capture_preimage_hash,
            self.source_session_id, self.source_thread_id,
            self.target_session_id, self.target_thread_id,
            self.checkpoint_ns, "pinned",
        ):
            raise RuntimeError("fork retention release 与冻结 capture 前像不一致")


def _record_from_row(row: sqlite3.Row) -> ForkRetentionReleaseRecord:
    values = dict(row)
    frozen = {key: value for key, value in values.items() if key not in {"state", "preimage_hash"}}
    if values["state"] not in {"preparing", "committed"} or (
        hashlib.sha256(canonical_json_bytes(frozen)).hexdigest() != values["preimage_hash"]
    ):
        raise RuntimeError("ForkRetentionReleaseRecord 冻结前像已损坏")
    return ForkRetentionReleaseRecord(**values)


def read_fork_retention_releases(control: SessionControlStore) -> tuple[ForkRetentionReleaseRecord, ...]:
    control._ensure_open()
    return tuple(_record_from_row(row) for row in control.connection.execute(
        "SELECT * FROM fork_retention_releases ORDER BY fork_retention_claim_id",
    ).fetchall())


def read_fork_retention_lease(
    control: SessionControlStore, capture: ForkSourceCaptureRecord, *, expected_generation: int,
) -> SessionOperationLease | None:
    """同 fork identity 的唯一 source lease，绑定原 capture/preimage 后才可恢复。"""
    lease = control.find_lease_by_operation("fork-retention:" + capture.fork_id)
    if lease is not None and (
        lease.operation_kind, lease.preimage_hash,
        lease.captured_lifecycle_generation, lease.recovery_ref,
    ) != ("fork_retention", capture.preimage_hash, expected_generation, capture.copy_operation_id):
        raise RuntimeError("fork retention source lease 与冻结 capture/generation 不一致")
    return lease


def prepare_fork_retention_release(
    control: SessionControlStore, *, claim: ForkRetentionClaim,
    capture: ForkSourceCaptureRecord, release_reason: str,
) -> ForkRetentionReleaseRecord:
    """target gate 内冻结精确 claim/capture；不读取或写入 source 控制库。"""
    request = json.loads(capture.request_json)
    if (claim.fork_retention_claim_id, claim.source_session_id, claim.target_session_id) != (
        capture.fork_id, capture.source_session_id, capture.target_session_id,
    ) or request["relationship"] != "pinned":
        raise RuntimeError("fork retention release 缺少匹配的 pinned capture record")
    if not release_reason:
        raise ValueError("fork retention release reason 不能为空")
    control.verify_matches_catalog_main_thread(request["target_main_thread_id"])
    frozen = {
        "fork_retention_claim_id": claim.fork_retention_claim_id,
        "workspace_id": claim.workspace_id,
        "source_session_id": capture.source_session_id,
        "source_thread_id": capture.source_thread_id,
        "target_session_id": capture.target_session_id,
        "target_thread_id": capture.target_thread_id,
        "source_lifecycle_generation": claim.source_lifecycle_generation,
        "copy_operation_id": capture.copy_operation_id,
        "capture_preimage_hash": capture.preimage_hash,
        "checkpoint_ns": request["checkpoint_ns"],
        "release_reason": release_reason,
    }
    record = ForkRetentionReleaseRecord(
        **frozen, preimage_hash=hashlib.sha256(canonical_json_bytes(frozen)).hexdigest(),
        state="preparing",
    )
    with control._write_transaction() as connection:
        row = connection.execute(
            "SELECT * FROM fork_retention_releases WHERE fork_retention_claim_id = ?",
            (record.fork_retention_claim_id,),
        ).fetchone()
        if row is not None:
            existing = _record_from_row(row)
            if replace(existing, state="preparing") != record:
                raise RuntimeError("fork retention release 同 claim 不同冻结前像")
            return existing
        connection.execute(
            "INSERT INTO fork_retention_releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing')",
            (*frozen.values(), record.preimage_hash),
        )
    return record


def commit_fork_retention_release(
    control: SessionControlStore, record: ForkRetentionReleaseRecord,
) -> ForkRetentionReleaseRecord:
    """source 精确 claim 已终结后，另取 target gate 才提交本地确认。"""
    with control._write_transaction() as connection:
        row = connection.execute(
            "SELECT * FROM fork_retention_releases WHERE fork_retention_claim_id = ?",
            (record.fork_retention_claim_id,),
        ).fetchone()
        if row is None:
            raise KeyError(record.fork_retention_claim_id)
        current = _record_from_row(row)
        if replace(current, state=record.state) != record:
            raise RuntimeError("fork retention release terminal CAS 前像冲突")
        if current.state == "committed":
            return current
        connection.execute(
            "UPDATE fork_retention_releases SET state = 'committed' "
            "WHERE fork_retention_claim_id = ? AND state = 'preparing'",
            (record.fork_retention_claim_id,),
        )
    return replace(record, state="committed")
