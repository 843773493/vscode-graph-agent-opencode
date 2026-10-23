"""fork 来源捕获 journal，负责冻结、物化、释放和未发布目标终结。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, fields
from typing import TYPE_CHECKING

from app.core.identifier import create_uuid_hex
from app.core.session_catalog_store.contracts import (
    ForkSourceCaptureRecord,
    SessionCreationRecord,
    fork_source_capture_relative_locator,
)
from app.core.session_catalog_store.creation_record_rows import (
    fetch_session_creation_record_row,
    session_creation_record_from_row,
)
from app.core.session_catalog_store.validators import (
    validate_operation_idempotency_key,
    validate_session_id,
    validate_thread_id,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME

if TYPE_CHECKING:
    from app.core.session_control_store import ThreadCreationRecord


class ForkSourceCaptureMixin:
    """fork source capture 方法族（唯一实现点）。"""

    @staticmethod
    def _fork_capture_from_row(row: sqlite3.Row) -> ForkSourceCaptureRecord:
        """只投影冻结 capture；目标本地释放状态不进入来源前像。"""
        return ForkSourceCaptureRecord(**{field.name: row[field.name] for field in fields(ForkSourceCaptureRecord)})

    def reserve_fork_source_capture(
        self, *, copy_operation_id: str, creation_key: str | None,
        source_session_id: str, source_thread_id: str,
        target_session_id: str, target_thread_id: str,
        request: dict[str, object], fork_id: str | None = None,
        materialization_id: str | None = None,
        snapshot_id: str | None = None, source_external: bool = False, target_external: bool = False,
    ) -> ForkSourceCaptureRecord:
        """capture 前一次性冻结 operation、snapshot locator 和物化 identity。"""
        validate_session_id(source_session_id)
        validate_session_id(target_session_id)
        validate_thread_id(source_thread_id)
        validate_thread_id(target_thread_id)
        validate_operation_idempotency_key(copy_operation_id)
        if source_external and target_external:
            raise ValueError("fork capture 必须由实际 source 或 target owner 登记")
        if snapshot_id is not None:
            if not isinstance(snapshot_id, str):
                raise TypeError("fork snapshot_id 必须是字符串")
            if len(snapshot_id) != 32 or any(char not in "0123456789abcdef" for char in snapshot_id):
                raise ValueError(f"fork snapshot_id 必须是 32 位小写 hex artifact ID: {snapshot_id!r}")
        request_json = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        preimage_hash = hashlib.sha256(request_json.encode()).hexdigest()
        with self.write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM fork_source_captures WHERE copy_operation_id = ?",
                (copy_operation_id,),
            ).fetchone()
            expected = (creation_key, source_session_id, source_thread_id,
                        target_session_id, target_thread_id, request_json, preimage_hash)
            if existing is not None:
                if tuple(existing[key] for key in (
                    "creation_key", "source_session_id", "source_thread_id", "target_session_id",
                    "target_thread_id", "request_json", "preimage_hash",
                )) != expected or (fork_id is not None and existing["fork_id"] != fork_id) or (
                    materialization_id is not None and existing["materialization_id"] != materialization_id
                ) or (
                    snapshot_id is not None and existing["snapshot_id"] != snapshot_id
                ):
                    raise RuntimeError("fork source capture 同 operation 不同 preimage，拒绝改绑")
                return self._fork_capture_from_row(existing)
            source_node = None if source_external else self._require_node(connection, source_session_id)
            if source_node is not None and source_node["kind"] != "session":
                raise RuntimeError("fork capture source 必须是 Session")
            if source_node is not None and source_node["state"] != "active":
                # lifecycle gate 依赖 catalog 的 ID 校验；在运行期拒绝分支取错误类型。
                from app.core.session_lifecycle_gate import SessionDeletionPendingError

                raise SessionDeletionPendingError(
                    "session_deletion_pending: source 正在删除，拒绝新 fork capture: "
                    f"session_id={source_session_id}, state={source_node['state']}"
                )
            if creation_key is not None and not target_external:
                if request["creation_kind"] == "new_child_thread":
                    target = self._require_node(connection, target_session_id)
                    if target["kind"] != "session" or target["state"] != "active" or not request["creation_preimage_hash"]:
                        raise RuntimeError("fork capture 与 preparing child 的 owner Session 不一致")
                else:
                    creation = fetch_session_creation_record_row(connection, creation_key)
                    if creation is None or (creation["session_id"], creation["main_thread_id"], creation["state"]) != (
                        target_session_id, target_thread_id, "preparing",
                    ):
                        raise RuntimeError("fork capture 与冻结 prepared creation 不一致")
            snapshot_id = snapshot_id or create_uuid_hex(namespace="fork/source_snapshot")
            connection.execute(
                "INSERT INTO fork_source_captures (copy_operation_id, creation_key, source_session_id, "
                "source_thread_id, target_session_id, target_thread_id, snapshot_id, "
                "snapshot_relative_locator, fork_id, materialization_id, request_json, preimage_hash, state, export_release_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?)",
                (copy_operation_id, *expected[:5], snapshot_id,
                 fork_source_capture_relative_locator(snapshot_id),
                 fork_id or create_uuid_hex(namespace="fork/identity"),
                 materialization_id or create_uuid_hex(namespace="fork/materialization"),
                  request_json, preimage_hash, "pending" if source_external else None),
            )
            return self._fork_capture_from_row(connection.execute(
                "SELECT * FROM fork_source_captures WHERE copy_operation_id = ?", (copy_operation_id,),
            ).fetchone())

    def get_fork_source_capture(
        self, *, copy_operation_id: str | None = None, creation_key: str | None = None,
        fork_id: str | None = None,
    ) -> ForkSourceCaptureRecord | None:
        """只按已知 operation/creation/fork key 定点读取，不扫描物理目录。"""
        selectors = {"copy_operation_id": copy_operation_id, "creation_key": creation_key, "fork_id": fork_id}
        selected = [(field, value) for field, value in selectors.items() if value is not None]
        if len(selected) != 1:
            raise ValueError("fork capture 必须且只能给定一个稳定 key")
        field, value = selected[0]
        with self.read_transaction() as connection:
            row = connection.execute(f"SELECT * FROM fork_source_captures WHERE {field} = ?", (value,)).fetchone()
        return None if row is None else self._fork_capture_from_row(row)

    def get_fork_export_release(self, copy_operation_id: str) -> tuple[str | None, str | None]:
        """读取目标清理 owner 的本地 delivery 事实，不读取来源。"""
        with self.read_transaction() as connection:
            row = connection.execute(
                "SELECT export_release_state, export_release_error FROM fork_source_captures WHERE copy_operation_id = ?",
                (copy_operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(copy_operation_id)
        return row[0], row[1]

    def list_pending_fork_export_captures(self) -> list[ForkSourceCaptureRecord]:
        """只枚举本地已终结且尚无真实释放回执的远端 capture。"""
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM fork_source_captures WHERE export_release_state = 'pending' "
                "AND state IN ('materialized', 'aborted') ORDER BY copy_operation_id",
            ).fetchall()
        return [self._fork_capture_from_row(row) for row in rows]

    def record_fork_export_release(
        self, record: ForkSourceCaptureRecord, *, receipt: ForkSourceCaptureRecord | None = None,
        error: str | None = None,
    ) -> None:
        """仅真实终结回执可确认；未知结果保存原错误并继续 pending。"""
        if (receipt is None) == (error is None):
            raise ValueError("fork export release 必须给定真实回执或原错误")
        receipt_json = None
        if receipt is not None:
            expected = asdict(record)
            actual = asdict(receipt)
            # partial import 中止时 target 未承诺 hash；来源回执保留自身已冻结 hash。
            hashes = {"snapshot_sha256", "marker_sha256"}
            if any(actual[key] != value for key, value in expected.items() if key not in {"state", "abort_reason", *hashes}) or any(
                expected[key] is not None and actual[key] != expected[key] for key in hashes
            ):
                raise RuntimeError("fork export release 回执冻结 identity 不一致")
            if record.state not in {"materialized", "aborted"} or receipt.state != record.state:
                raise RuntimeError("fork export release 回执与目标终态不一致")
            if record.state == "materialized" and any(expected[key] is None for key in hashes):
                raise RuntimeError("fork export release materialized 目标缺少冻结 hash")
            receipt_json = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT preimage_hash, state, export_release_state, export_release_receipt_json "
                "FROM fork_source_captures WHERE copy_operation_id = ?", (record.copy_operation_id,),
            ).fetchone()
            if row is None or row["preimage_hash"] != record.preimage_hash or row["state"] != record.state:
                raise RuntimeError("fork export release 目标 journal identity/终态冲突")
            if row["export_release_state"] is None:
                raise RuntimeError("fork export release 只能由远端 capture 的目标 owner 登记")
            if row["export_release_state"] == "confirmed":
                if receipt_json is not None and receipt_json != row["export_release_receipt_json"]:
                    raise RuntimeError("fork export release 已确认回执冲突")
                return
            connection.execute(
                "UPDATE fork_source_captures SET export_release_state = ?, export_release_error = ?, "
                "export_release_receipt_json = ? WHERE copy_operation_id = ? AND export_release_state = 'pending'",
                ("confirmed" if receipt is not None else "pending", error, receipt_json, record.copy_operation_id),
            )

    def mark_fork_source_captured(
        self, record: ForkSourceCaptureRecord, *, snapshot_sha256: str, marker_sha256: str,
    ) -> ForkSourceCaptureRecord:
        """marker durability 完成且 source guard 释放后 CAS；不得换 source revision。"""
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fork_source_captures WHERE copy_operation_id = ?", (record.copy_operation_id,),
            ).fetchone()
            if row is None or row["preimage_hash"] != record.preimage_hash:
                raise RuntimeError("fork source capture CAS preimage 不一致")
            if row["state"] in {"source_captured", "materialized"}:
                if (row["snapshot_sha256"], row["marker_sha256"]) != (snapshot_sha256, marker_sha256):
                    raise RuntimeError("fork source captured hash 冲突")
            elif row["state"] == "preparing":
                connection.execute(
                    "UPDATE fork_source_captures SET state = 'source_captured', snapshot_sha256 = ?, "
                    "marker_sha256 = ? WHERE copy_operation_id = ? AND state = 'preparing'",
                    (snapshot_sha256, marker_sha256, record.copy_operation_id),
                )
            else:
                raise RuntimeError("fork source capture 已终结，拒绝恢复")
            return self._fork_capture_from_row(connection.execute(
                "SELECT * FROM fork_source_captures WHERE copy_operation_id = ?", (record.copy_operation_id,),
            ).fetchone())

    def finish_fork_source_capture(self, copy_operation_id: str, *, state: str, reason: str | None = None) -> None:
        """物化完成或定点回滚终结 capture；终态不得被迟到 callback 复活。"""
        if state not in {"materialized", "aborted"}:
            raise ValueError("fork source capture 终态非法")
        with self.write_transaction() as connection:
            row = connection.execute("SELECT state FROM fork_source_captures WHERE copy_operation_id = ?", (copy_operation_id,)).fetchone()
            if row is None:
                raise KeyError(copy_operation_id)
            if row[0] == state:
                return
            if row[0] in {"materialized", "aborted"} or (state == "materialized" and row[0] != "source_captured"):
                raise RuntimeError("fork source capture 终态 CAS 冲突")
            connection.execute("UPDATE fork_source_captures SET state = ?, abort_reason = ? WHERE copy_operation_id = ?", (state, reason, copy_operation_id))

    def abort_unpublished_fork_capture(
        self, capture: ForkSourceCaptureRecord, *, creation: SessionCreationRecord | ThreadCreationRecord, reason: str,
    ) -> None:
        """调用方持有目标 gate；先关闭 child 控制库，再提交 workspace 事务。"""
        from app.core.session_control_store import (
            SessionControlStore,
            ThreadCreationRecord,
        )

        if capture.creation_key is None:
            raise RuntimeError("fork abort 缺少 prepared creation capability")
        request = json.loads(capture.request_json)
        if (capture.copy_operation_id != capture.creation_key
            or hashlib.sha256(capture.request_json.encode()).hexdigest() != capture.preimage_hash):
            raise RuntimeError("fork abort 的 capture operation/preimage 与原创建 owner 冲突")
        child_owner = None
        if isinstance(creation, ThreadCreationRecord):
            if (request["creation_kind"], capture.creation_key, capture.target_thread_id,
                request["creation_preimage_hash"], creation.purpose, creation.state) != (
                "new_child_thread", creation.thread_creation_idempotency_key, creation.child_thread_id,
                creation.preimage_hash, "fork", "preparing",
            ):
                raise RuntimeError("fork child abort 与原 creation owner 冲突")
            child_owner = self.get_node(capture.target_session_id)
            if child_owner.kind != "session" or child_owner.state not in {"active", "deleting"}:
                raise RuntimeError("fork child abort 的 owner Session 已隔离或丢失")
            control = SessionControlStore(self.resolve_session_locator(child_owner.storage_relative_locator) / CONTROL_DATABASE_NAME, read_only=True)
            try:
                if control.get_thread_creation_record(capture.creation_key) != creation:
                    raise RuntimeError("fork child target 已发布/终结或创建 owner 冲突，禁止 capture abort")
            finally:
                control.close()
        with self.write_transaction() as connection:
            row = connection.execute("SELECT * FROM fork_source_captures WHERE copy_operation_id = ?", (capture.copy_operation_id,)).fetchone()
            if row is None or self._fork_capture_from_row(row) != capture:
                raise RuntimeError("fork abort 的冻结 capture 已变化")
            if child_owner is not None:
                target = self._require_node(connection, row["target_session_id"])
                if (target["kind"] != "session" or target["state"] not in {"active", "deleting"}
                    or target["storage_relative_locator"] != child_owner.storage_relative_locator):
                    raise RuntimeError("fork child abort 的 owner Session 已隔离或丢失")
            else:
                current = fetch_session_creation_record_row(connection, row["creation_key"])
                if (not isinstance(creation, SessionCreationRecord) or current is None
                    or session_creation_record_from_row(current) != creation or request["creation_kind"] != "new_session_main"
                    or (creation.purpose, creation.state, creation.session_id, creation.main_thread_id, creation.preimage_hash) != (
                        "context_fork", "preparing", capture.target_session_id, capture.target_thread_id, request["creation_preimage_hash"],
                    )):
                    raise RuntimeError("fork target 已发布/终结或创建 owner 冲突，禁止 capture abort")
            connection.execute("UPDATE fork_source_captures SET state = 'aborted', abort_reason = ? WHERE copy_operation_id = ?", (reason, capture.copy_operation_id))
