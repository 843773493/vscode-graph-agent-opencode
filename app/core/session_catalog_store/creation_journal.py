"""session_creation_records 一条垂直链路（8.1-A 创建流 journal）。

承载创建 record 的行投影、create-or-get（同 key 同 preimage 幂等、不同
preimage 冲突）、冻结父 revision、CAS 发布（唯一可见性提交点：同事务插入
可见 node）与 preparing→aborted 终结。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session_control_store import ThreadCreationRecord
    from app.core.session_control_store.contracts import ThreadGraphBindingRecord

from app.core.identifier import (
    create_prefixed_id,
    create_uuid_hex,
    to_epoch_ms,
    uuid7_datetime_from_hex,
)
from app.core.session_catalog_store._schema import _CREATION_RECORD_COLUMNS
from app.core.session_catalog_store.contracts import (
    ForkSourceCaptureRecord,
    SessionCatalogNode,
    SessionCreationPurpose,
    SessionCreationRecord,
    fork_source_capture_relative_locator,
)
from app.core.session_catalog_store.validators import (
    validate_session_id,
    validate_session_storage_identity,
    validate_thread_id,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME


class CreationJournalMixin:
    """creation record journal 方法族（唯一实现点）。"""

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
        self._validate_idempotency_key(copy_operation_id)
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
                    creation = self._fetch_creation_record(connection, creation_key)
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
                current = self._fetch_creation_record(connection, row["creation_key"])
                if (not isinstance(creation, SessionCreationRecord) or current is None
                    or self._creation_record_from_row(current) != creation or request["creation_kind"] != "new_session_main"
                    or (creation.purpose, creation.state, creation.session_id, creation.main_thread_id, creation.preimage_hash) != (
                        "context_fork", "preparing", capture.target_session_id, capture.target_thread_id, request["creation_preimage_hash"],
                    )):
                    raise RuntimeError("fork target 已发布/终结或创建 owner 冲突，禁止 capture abort")
            connection.execute("UPDATE fork_source_captures SET state = 'aborted', abort_reason = ? WHERE copy_operation_id = ?", (reason, capture.copy_operation_id))

    @staticmethod
    def _fetch_creation_record(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
            "WHERE session_creation_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _creation_record_from_row(row: sqlite3.Row) -> SessionCreationRecord:
        return SessionCreationRecord(
            session_creation_idempotency_key=str(
                row["session_creation_idempotency_key"]
            ),
            session_id=str(row["session_id"]),
            main_thread_id=str(row["main_thread_id"]),
            workspace_id=str(row["workspace_id"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            display_name=str(row["display_name"]),
            title_source=str(row["title_source"]),
            purpose=str(row["purpose"]),
            graph_id=str(row["graph_id"]),
            graph_revision=int(row["graph_revision"]),
            graph_schema_hash=str(row["graph_schema_hash"]),
            capability_profile_hash=str(row["capability_profile_hash"]),
            session_metadata_json=str(row["session_metadata_json"]),
            request_intent_json=(
                str(row["request_intent_json"])
                if row["request_intent_json"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            storage_relative_locator=str(row["storage_relative_locator"]),
            preimage_hash=str(row["preimage_hash"]),
            parent_revision=(
                int(row["parent_revision"])
                if row["parent_revision"] is not None
                else None
            ),
            state=str(row["state"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
            fork_delivery_request_id=row["fork_delivery_request_id"],
        )

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str):
            raise TypeError(
                f"idempotency_key 必须是字符串: {idempotency_key!r}"
            )
        if not idempotency_key:
            raise ValueError("idempotency_key 不能为空")

    def create_or_get_creation_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        title_source: str = "user",
        purpose: SessionCreationPurpose,
        graph_binding: ThreadGraphBindingRecord,
        session_metadata_json: str,
        preimage_hash: str,
        request_intent_json: str | None = None,
        fork_delivery_request_id: str | None = None,
        created_at: datetime | None = None,
        session_id: str | None = None,
        main_thread_id: str | None = None,
    ) -> SessionCreationRecord:
        """create-or-get 创建流 journal record（gate 内短事务，8.1-A）。

        - 同 key 已存在：``preimage_hash`` 一致 → 返回既有 record（幂等，
          created_at 等冻结值以既有 record 为准）；不一致 → ``RuntimeError``
          （同 key 不同 preimage 冲突）。
        - 实时创建省略 ``created_at`` 与固定 ID：session/main-thread ID 均走
          uuid-utils 默认 UUIDv7 分配，``created_at`` 与 session locator 日期
          只从 session ID 内嵌毫秒推导。
        - 固定历史 fixture 必须同时传入 ``session_id``、``main_thread_id`` 与
          ``created_at``，两个 ID 的内嵌毫秒都须与给定时刻相同；journal 不按
          显式时刻生成 ID。
        - 幂等命中直接复用冻结 record，不重新分配 ID 或时间。新 record 插入
          ``state='preparing'``，不发布可见 node。
        - parent 校验：存在、非 deleting、同 workspace（复用
          :meth:`_require_mutable_parent`）；同时预检 session_id /
          main_thread_id / locator 未被既有 nodes 行占用（fail fast，
          UNIQUE 约束兜底）。
        """
        from app.core.session_control_store.contracts import ThreadGraphBindingRecord

        if not isinstance(graph_binding, ThreadGraphBindingRecord):
            raise TypeError("graph_binding 必须是 ThreadGraphBindingRecord")
        self._validate_idempotency_key(idempotency_key)
        self._validate_common_fields(workspace_id, display_name)
        if title_source not in ("default", "user", "auto"):
            raise ValueError(f"title_source 非法: {title_source!r}")
        if purpose not in ("normal", "context_fork"):
            raise ValueError(f"session creation purpose 非法: {purpose!r}")
        if fork_delivery_request_id is not None:
            if not isinstance(fork_delivery_request_id, str):
                raise TypeError("fork delivery request_id 必须是字符串")
            if purpose != "context_fork" or not fork_delivery_request_id.strip():
                raise ValueError("只有 context fork 可以冻结非空 delivery request_id")
        if not isinstance(session_metadata_json, str) or not session_metadata_json:
            raise ValueError("session_metadata_json 不能为空")
        try:
            session_metadata = json.loads(session_metadata_json)
        except ValueError as error:
            raise ValueError("session_metadata_json 必须是 JSON 对象") from error
        if not isinstance(session_metadata, dict):
            raise ValueError(  # noqa: TRY004 - JSON 根类型属于序列化形态错误。
                "session_metadata_json 必须是 JSON 对象"
            )
        canonical_metadata = json.dumps(
            session_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical_metadata != session_metadata_json:
            raise ValueError("session_metadata_json 必须使用 canonical JSON")
        if request_intent_json is not None:
            request_intent = json.loads(request_intent_json)
            if not isinstance(request_intent, dict):
                raise ValueError("request_intent_json 必须是 JSON 对象")
            if json.dumps(request_intent, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) != request_intent_json:
                raise ValueError("request_intent_json 必须使用 canonical JSON")
        if created_at is not None and not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at is not None and created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        if not isinstance(preimage_hash, str) or not preimage_hash:
            raise ValueError(f"preimage_hash 不能为空: {preimage_hash!r}")
        fixture_values = (created_at, session_id, main_thread_id)
        if any(value is not None for value in fixture_values) and any(
            value is None for value in fixture_values
        ):
            raise ValueError(
                "固定 Session fixture 必须同时提供 created_at、session_id 与 "
                "main_thread_id"
            )
        if session_id is not None:
            validate_session_id(session_id)
        if main_thread_id is not None:
            validate_thread_id(main_thread_id)
        if created_at is not None:
            if session_id is None or main_thread_id is None:
                raise ValueError(
                    "固定 Session fixture 必须同时提供 created_at、session_id 与 "
                    "main_thread_id"
                )
            expected_ms = to_epoch_ms(created_at)
            session_ms = to_epoch_ms(uuid7_datetime_from_hex(session_id[4:]))
            main_thread_ms = to_epoch_ms(
                uuid7_datetime_from_hex(main_thread_id[4:])
            )
            if session_ms != expected_ms or main_thread_ms != expected_ms:
                raise ValueError(
                    "固定 Session fixture 的 created_at 必须与 session_id 和 "
                    "main_thread_id 内嵌时间戳一致: "
                    f"created_at_ms={expected_ms}, session_ms={session_ms}, "
                    f"main_thread_ms={main_thread_ms}"
                )
        with self.write_transaction() as connection:
            existing = self._fetch_creation_record(connection, idempotency_key)
            if existing is not None:
                if tuple(existing[field] for field in (
                    "graph_id", "graph_revision", "graph_schema_hash", "capability_profile_hash",
                )) != graph_binding.as_tuple():
                    raise RuntimeError(
                        "session creation record GraphBinding preimage 冲突（同 key 不同 selector，拒绝复用）: "
                        f"key={idempotency_key!r}"
                    )
                if str(existing["preimage_hash"]) != preimage_hash:
                    raise RuntimeError(
                        "session creation record preimage 冲突（同 key 不同 "
                        "preimage，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={existing['preimage_hash']!r}, "
                        f"requested_preimage={preimage_hash!r}"
                    )
                if (
                    session_id is not None
                    and str(existing["session_id"]) != session_id
                ):
                    raise RuntimeError(
                        "session creation record session_id 冲突（同 key 幂等"
                        "复用时传入 ID 与既有 record 不一致，拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_session_id={existing['session_id']!r}, "
                        f"requested_session_id={session_id!r}"
                    )
                if (
                    main_thread_id is not None
                    and str(existing["main_thread_id"]) != main_thread_id
                ):
                    raise RuntimeError(
                        "session creation record main_thread_id 冲突（同 key 幂等"
                        "复用时传入 ID 与既有 record 不一致，拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_main_thread_id={existing['main_thread_id']!r}, "
                        f"requested_main_thread_id={main_thread_id!r}"
                    )
                if str(existing["title_source"]) != title_source:
                    raise RuntimeError(
                        "session creation record title_source 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_title_source={existing['title_source']!r}, "
                        f"requested_title_source={title_source!r}"
                    )
                if str(existing["purpose"]) != purpose:
                    raise RuntimeError(
                        "session creation record purpose 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_purpose={existing['purpose']!r}, "
                        f"requested_purpose={purpose!r}"
                    )
                if str(existing["session_metadata_json"]) != session_metadata_json:
                    raise RuntimeError(
                        "session creation record metadata 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}"
                    )
                if existing["request_intent_json"] != request_intent_json:
                    raise RuntimeError(
                        "session creation record 请求意图冲突（同 key 不同意图，拒绝复用）: "
                        f"key={idempotency_key!r}"
                    )
                return self._creation_record_from_row(existing)
            # 插入路径：事务内先验证后写入。
            self._require_mutable_parent(connection, parent_node_id, workspace_id)
            parent_revision: int | None = None
            if parent_node_id is not None:
                parent_row = self._fetch_node(connection, parent_node_id)
                if parent_row is None:
                    # 防御性兜底：_require_mutable_parent 刚验证过存在。
                    raise KeyError(f"会话目录节点不存在: {parent_node_id}")
                parent_revision = int(parent_row["revision"])
            if session_id is None:
                allocated_session_id = create_prefixed_id("ses")
                allocated_main_thread_id = create_prefixed_id("thr")
                allocated_created_at = uuid7_datetime_from_hex(
                    allocated_session_id[4:]
                )
            else:
                if created_at is None or main_thread_id is None:
                    raise RuntimeError("Session journal 固定 fixture 字段缺失")
                allocated_session_id = session_id
                allocated_main_thread_id = main_thread_id
                allocated_created_at = created_at
            validate_session_id(allocated_session_id)
            validate_thread_id(allocated_main_thread_id)
            utc_date = allocated_created_at.astimezone(UTC).date()
            locator = f"sessions/{utc_date:%Y/%m/%d}/{allocated_session_id}"
            validate_session_storage_identity(allocated_session_id, allocated_created_at, locator)
            self._validate_locator_budget(locator)
            # fail fast：新分配身份不得与既有可见 node 冲突（UNIQUE 兜底）。
            self._require_node_id_available(connection, allocated_session_id)
            self._require_unique_session_fields(
                connection, workspace_id, locator, allocated_main_thread_id
            )
            record_created_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO session_creation_records ("
                "session_creation_idempotency_key, session_id, main_thread_id, "
                "workspace_id, parent_node_id, display_name, title_source, purpose, "
                "graph_id, graph_revision, graph_schema_hash, capability_profile_hash, "
                "session_metadata_json, request_intent_json, created_at, storage_relative_locator, "
                "preimage_hash, parent_revision, state, abort_reason, "
                "record_created_at, record_updated_at, fork_delivery_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', "
                "NULL, ?, ?, ?)",
                (
                    idempotency_key,
                    allocated_session_id,
                    allocated_main_thread_id,
                    workspace_id,
                    parent_node_id,
                    display_name,
                    title_source,
                    purpose,
                    *graph_binding.as_tuple(),
                    session_metadata_json,
                    request_intent_json,
                    allocated_created_at.isoformat(),
                    locator,
                    preimage_hash,
                    parent_revision,
                    record_created_at,
                    record_created_at,
                    fork_delivery_request_id,
                ),
            )
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "creation record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def get_creation_record(self, idempotency_key: str) -> SessionCreationRecord:
        """按幂等键返回 creation record 投影；不存在抛 KeyError。"""
        self._validate_idempotency_key(idempotency_key)
        with self.read_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def list_preparing_creation_records(
        self,
        *,
        workspace_id: str,
        purpose: SessionCreationPurpose,
    ) -> list[SessionCreationRecord]:
        """按冻结 workspace 与 purpose 列出未发布创建记录，供启动恢复使用。"""
        self._validate_common_fields(workspace_id, "session")
        if purpose not in ("normal", "context_fork"):
            raise ValueError(f"session creation purpose 非法: {purpose!r}")
        with self.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
                "WHERE workspace_id = ? AND purpose = ? AND state = 'preparing' "
                "ORDER BY record_created_at, session_creation_idempotency_key",
                (workspace_id, purpose),
            ).fetchall()
            return [self._creation_record_from_row(row) for row in rows]

    def list_published_fork_creation_records(self) -> list[SessionCreationRecord]:
        """只由权威 catalog 枚举公开 active fork；删除中的 target 归删除 owner。"""
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT c.* FROM session_creation_records c JOIN nodes n ON n.node_id = c.session_id "
                "JOIN fork_source_captures f ON f.creation_key = c.session_creation_idempotency_key "
                "WHERE c.purpose = 'context_fork' AND c.state = 'published' AND n.state = 'active' "
                "AND f.state = 'materialized' ORDER BY c.session_creation_idempotency_key"
            ).fetchall()
        return [self._creation_record_from_row(row) for row in rows]

    def publish_creation_record(
        self,
        idempotency_key: str,
        *,
        expected_purpose: SessionCreationPurpose,
    ) -> SessionCatalogNode:
        """CAS 发布 creation record 并插入可见 node（**唯一可见性提交点**）。

        单事务内依次验证：record 存在且 ``state='preparing'``；record 内部
        一致性（ID/locator 形态、locator 叶名与日期）；父节点仍存在、
        active 且 revision 等于冻结 ``parent_revision``（漂移/缺失/删除 →
        RuntimeError，含期望/实际）；目标 session_id / main_thread_id /
        locator 未被其它 nodes 行占用（显式预检 + UNIQUE 兜底）。随后同一
        事务内插入 nodes 行（等价 create_session_node 语义，身份字段全部
        来自 record 冻结值）并把 record 推进为 ``published``。任何失败回滚
        整个事务：node 不发布、record 保持 preparing。
        """
        self._validate_idempotency_key(idempotency_key)
        if expected_purpose not in ("normal", "context_fork"):
            raise ValueError(
                f"session creation purpose 非法: {expected_purpose!r}"
            )
        with self.write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if str(row["purpose"]) != expected_purpose:
                raise RuntimeError(
                    "session creation publish purpose 与 durable record 不一致: "
                    f"key={idempotency_key!r}, expected={expected_purpose!r}, "
                    f"actual={row['purpose']!r}"
                )
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，拒绝重复发布: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，拒绝发布: "
                    f"key={idempotency_key!r}, "
                    f"abort_reason={row['abort_reason']!r}"
                )
            session_id = str(row["session_id"])
            main_thread_id = str(row["main_thread_id"])
            workspace_id = str(row["workspace_id"])
            parent_node_id = (
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            )
            display_name = str(row["display_name"])
            title_source = str(row["title_source"])
            created_at_text = str(row["created_at"])
            locator = str(row["storage_relative_locator"])
            try:
                created_at = datetime.fromisoformat(created_at_text)
            except ValueError as error:
                raise RuntimeError(
                    "creation record created_at 无法解析（record 被外部改动，"
                    f"fail closed）: {created_at_text!r}: {error}"
                ) from error
            # record 内部一致性复验（防绕过软件直改 record）。
            validate_session_id(session_id)
            if title_source not in ("default", "user", "auto"):
                raise RuntimeError(
                    "creation record title_source 非法（record 被外部改动，"
                    f"fail closed）: {title_source!r}"
                )
            validate_thread_id(main_thread_id)
            validate_session_storage_identity(session_id, created_at, locator)
            self._validate_locator_budget(locator)
            # 父节点 CAS：仍存在、active、revision 未漂移。
            if parent_node_id is not None:
                parent = self._fetch_node(connection, parent_node_id)
                if parent is None:
                    raise RuntimeError(
                        "session creation publish 失败：父节点已不存在"
                        f"（operation 须 abort 后换新 key 重试）: "
                        f"key={idempotency_key!r}, parent={parent_node_id}"
                    )
                if str(parent["state"]) != "active":
                    raise RuntimeError(
                        "session creation publish 失败：父节点非 active: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"parent_state={parent['state']!r}"
                    )
                frozen_revision = row["parent_revision"]
                if frozen_revision is None:
                    raise RuntimeError(
                        "creation record 冻结 parent_revision 缺失（record 被"
                        f"外部改动，fail closed）: key={idempotency_key!r}"
                    )
                actual_revision = int(parent["revision"])
                if actual_revision != int(frozen_revision):
                    raise RuntimeError(
                        "session creation publish 失败：父节点 revision 已漂移: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"expected_revision={int(frozen_revision)}, "
                        f"actual_revision={actual_revision}"
                    )
            # 冻结 intent 只在首次发布时初始化 nodes；后续读取与业务更新不回读 journal。
            metadata = json.loads(row["session_metadata_json"])
            origin = metadata["generation_origin"]
            node = self.create_session_node(
                session_id, workspace_id, parent_node_id, display_name,
                created_at, locator, main_thread_id, title_source,
                current_agent_id=metadata["current_agent_id"],
                current_provider_id=metadata["current_provider_id"],
                session_kind=metadata["kind"],
                context_source_session_id=metadata["context_source_session_id"],
                generation_origin_json=(
                    json.dumps(origin, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if origin is not None else None
                ),
                connection=connection,
            )
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            return node

    def abort_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SessionCreationRecord:
        """终结 creation record：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。
        """
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self.write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._creation_record_from_row(row)
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_creation_record(connection, idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(updated)
