"""同一 Session collaboration board/event/fanout 的权威事务与恢复记录。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store.validators import (
    validate_session_id,
    validate_thread_id,
)
from app.core.session_control_thread_catalog.thread_catalog import read_fence_row

COLLABORATION_STATE_DDL = (
    """CREATE TABLE IF NOT EXISTS collaboration_boards (
        team_id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version >= 1),
        board_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collaboration_events (
        event_id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES collaboration_boards(team_id),
        ledger_revision INTEGER NOT NULL UNIQUE, event_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collaboration_fanouts (
        ledger_revision INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
        event_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        lease_id TEXT NOT NULL UNIQUE REFERENCES session_operation_leases(lease_id),
        wakeup_json TEXT, wakeup_receipt_json TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','completed','cancelled')),
        created_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS collaboration_fanouts_pending
        ON collaboration_fanouts(state, ledger_revision) WHERE state='pending'""",
    """CREATE TABLE IF NOT EXISTS collaboration_deliveries (
        ledger_revision INTEGER NOT NULL REFERENCES collaboration_fanouts(ledger_revision),
        recipient_thread_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','delivered','recipient_deleted','fence_closed')),
        error TEXT, PRIMARY KEY(ledger_revision, recipient_thread_id))""",
    """CREATE INDEX IF NOT EXISTS collaboration_deliveries_pending
        ON collaboration_deliveries(recipient_thread_id, ledger_revision) WHERE state='pending'""",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CollaborationDelivery:
    thread_id: str
    state: str
    error: str | None


@dataclass(frozen=True, slots=True)
class CollaborationFanoutRecord:
    ledger_revision: int
    session_id: str
    event_hash: str
    payload_json: str
    lease_id: str
    wakeup_json: str | None
    wakeup_receipt_json: str | None
    state: str
    recipients: tuple[CollaborationDelivery, ...]


class CollaborationFanoutMixin:
    """board、成员 publication 与逐 recipient 状态复用同一 control owner。"""

    def _freeze_collaboration_change_transaction(
        self, connection: sqlite3.Connection, *, session_id: str,
        event: dict[str, object], wakeup: dict[str, object] | None = None,
        bump_revision: bool = True,
    ) -> int:
        if connection is not self._connection or not connection.in_transaction:
            raise RuntimeError("collaboration fanout 必须在原 ledger 事务中冻结")
        fence = read_fence_row(connection, database_path=self.database_path)
        if fence["state"] != "active":
            raise RuntimeError("collaboration ledger 的 lifecycle fence 已关闭")
        if bump_revision:
            connection.execute("UPDATE collaboration_ledger SET revision=revision+1 WHERE id=1")
        revision = self.get_collaboration_ledger_revision()
        members = [dict(row) for row in connection.execute(
            "SELECT m.*, i.state AS admission_state FROM collaboration_members m "
            "LEFT JOIN thread_execution_intents i ON i.thread_id=m.child_thread_id "
            "ORDER BY m.registered_at,m.delegation_id",
        )]
        boards = [json.loads(row[0]) for row in connection.execute(
            "SELECT board_json FROM collaboration_boards ORDER BY team_id",
        )]
        main = self.get_main_thread()["thread_id"]
        recipients = tuple(sorted({str(main), *(str(member["child_thread_id"]) for member in members
                                               if member["state"] == "published")}))
        if any(member["coordinator_session_id"] != session_id for member in members):
            raise RuntimeError("collaboration member 与冻结 Session owner 不一致")
        if any(thread == "None" for thread in recipients):
            raise RuntimeError("published collaboration member 缺少精确 child thread")
        for thread in recipients:
            validate_thread_id(thread)
            if connection.execute("SELECT 1 FROM thread_catalog WHERE thread_id=?", (thread,)).fetchone() is None:
                raise RuntimeError(f"collaboration recipient 不在权威 thread catalog: {thread}")
        if wakeup is not None:
            CollaborationFanoutMixin._validate_wakeup(wakeup, recipients)
        event_hash = hashlib.sha256(_json(event).encode()).hexdigest()
        payload = _json({"session_id": session_id, "ledger_revision": revision,
                         "event_hash": event_hash, "event": event, "members": members, "boards": boards})
        lease = self.create_or_get_lease(
            operation_kind="collaboration_fanout", operation_identity=f"collaboration:{revision}",
            preimage_hash=hashlib.sha256(_json({"payload": payload, "recipients": recipients,
                                              "wakeup": wakeup}).encode()).hexdigest(),
            expected_generation=int(fence["generation"]), recovery_ref=f"collaboration:{revision}",
            transaction_connection=connection,
        )
        connection.execute(
            "INSERT INTO collaboration_fanouts VALUES (?,?,?,?,?,?,NULL,'pending',?)",
            (revision, session_id, event_hash, payload, lease.lease_id,
             None if wakeup is None else _json(wakeup), datetime.now(UTC).isoformat()),
        )
        connection.executemany("INSERT INTO collaboration_deliveries VALUES (?,?,'pending',NULL)",
                               ((revision, thread) for thread in recipients))
        self._collaboration_notifications.append(revision)
        return revision

    def _freeze_collaboration_member_change_transaction(
        self, connection: sqlite3.Connection, delegation_id: str, event_type: str,
        *, bump_revision: bool = True,
    ) -> None:
        member = self.get_collaboration_member(delegation_id)
        if read_fence_row(connection, database_path=self.database_path)["state"] != "active" and event_type == "member.cancelled":
            if member.state != "cancelled":
                raise RuntimeError("关闭 fence 的成员终结必须对应原 cancelled member")
            # 删除只结算原创建；不在关闭的 fence 下接受新 fanout lease。
            # 已冻结的投递由原删除 drain 终结，不能向 Thread 晚到 append。
            if bump_revision:
                connection.execute("UPDATE collaboration_ledger SET revision=revision+1 WHERE id=1")
            return
        self._freeze_collaboration_change_transaction(
            connection, session_id=member.coordinator_session_id,
            event={"type": event_type, "delegation_id": delegation_id}, bump_revision=bump_revision,
        )

    def commit_collaboration_board(
        self, *, session_id: str, team_id: str, version: int,
        board_json: str, event_json: str, wakeup_json: str | None = None,
    ) -> int:
        validate_session_id(session_id)
        board, event = json.loads(board_json), json.loads(event_json)
        if (board["session_id"], board["team_id"], board["version"]) != (session_id, team_id, version):
            raise ValueError("collaboration board owner/version 与提交字段不一致")
        if (event["session_id"], event["team_id"]) != (session_id, team_id):
            raise ValueError("collaboration event owner 与 board 不一致")
        if board["members"] or board["recent_events"]:
            raise ValueError("board 不得存入第二份成员或事件权威")
        wakeup = None if wakeup_json is None else json.loads(wakeup_json)
        with self._write_transaction() as connection:
            existing_event = connection.execute(
                "SELECT ledger_revision,event_json FROM collaboration_events WHERE event_id=?", (event["event_id"],),
            ).fetchone()
            if existing_event is not None:
                original = self.get_collaboration_fanout(int(existing_event[0]))
                original_board = next(value for value in json.loads(original.payload_json)["boards"] if value["team_id"] == team_id)
                if _json(original_board) != _json(board) or _json(json.loads(existing_event[1])) != _json(event) or original.wakeup_json != (None if wakeup is None else _json(wakeup)):
                    raise RuntimeError("collaboration event 幂等 preimage 冲突")
                return int(existing_event[0])
            current = connection.execute("SELECT version FROM collaboration_boards WHERE team_id=?", (team_id,)).fetchone()
            if (0 if current is None else int(current[0])) != version - 1:
                raise RuntimeError(f"collaboration board version CAS 冲突: team_id={team_id}, incoming={version}")
            connection.execute("INSERT INTO collaboration_boards VALUES (?,?,?) ON CONFLICT(team_id) "
                               "DO UPDATE SET version=excluded.version,board_json=excluded.board_json",
                               (team_id, version, _json(board)))
            revision = self._freeze_collaboration_change_transaction(connection, session_id=session_id, event=event, wakeup=wakeup)
            connection.execute("INSERT INTO collaboration_events VALUES (?,?,?,?)",
                               (event["event_id"], team_id, revision, _json(event)))
            return revision

    def collaboration_boards(self, team_id: str | None = None) -> tuple[str, ...]:
        self._ensure_open()
        return tuple(str(row[0]) for row in self._connection.execute(
            "SELECT board_json FROM collaboration_boards" + (" WHERE team_id=?" if team_id is not None else " ORDER BY team_id"),
            () if team_id is None else (team_id,),
        ))

    def collaboration_recent_events(self, team_id: str, limit: int) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("collaboration event limit 必须大于0")
        self._ensure_open()
        rows = self._connection.execute("SELECT event_json FROM collaboration_events WHERE team_id=? "
                                        "ORDER BY ledger_revision DESC LIMIT ?", (team_id, limit)).fetchall()
        return tuple(str(row[0]) for row in reversed(rows))

    def collaboration_latest_task_event(self, team_id: str, task_id: str) -> str | None:
        """精确读取原任务的末次事件；恢复判断不依赖面板最近20条展示页。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT event_json FROM collaboration_events WHERE team_id=? "
            "AND json_extract(event_json,'$.payload.task_id')=? ORDER BY ledger_revision DESC LIMIT 1",
            (team_id, task_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def get_collaboration_fanout(self, revision: int) -> CollaborationFanoutRecord:
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM collaboration_fanouts WHERE ledger_revision=?", (revision,)).fetchone()
        if row is None:
            raise KeyError(f"collaboration fanout 不存在: revision={revision}")
        record = CollaborationFanoutRecord(
            revision, str(row["session_id"]), str(row["event_hash"]), str(row["payload_json"]),
            str(row["lease_id"]), row["wakeup_json"], row["wakeup_receipt_json"], str(row["state"]),
            tuple(CollaborationDelivery(str(item[0]), str(item[1]), item[2]) for item in self._connection.execute(
                "SELECT recipient_thread_id,state,error FROM collaboration_deliveries WHERE ledger_revision=? ORDER BY recipient_thread_id", (revision,),
            )),
        )
        CollaborationFanoutMixin._validate_fanout(record, self.get_lease(record.lease_id))
        return record

    @staticmethod
    def _validate_wakeup(wakeup: dict[str, object], recipients: tuple[str, ...]) -> None:
        if set(wakeup) != {"target_thread_id", "source_id", "idempotency_key", "content", "metadata"}:
            raise ValueError("collaboration wakeup 字段不完整或包含未知字段")
        if any(not isinstance(wakeup[key], str) or not wakeup[key]
               for key in ("target_thread_id", "source_id", "idempotency_key", "content")):
            raise ValueError("collaboration wakeup identity/content 必须为非空字符串")
        if not isinstance(wakeup["metadata"], dict) or wakeup["target_thread_id"] not in recipients:
            raise ValueError("collaboration wakeup 必须指向原冻结 recipient")

    @staticmethod
    def _validate_fanout(record: CollaborationFanoutRecord, lease) -> None:
        """读取也核冻结 preimage；损坏不能变成新目标或虚假 ready。"""
        validate_session_id(record.session_id)
        payload = json.loads(record.payload_json)
        if set(payload) != {"session_id", "ledger_revision", "event_hash", "event", "members", "boards"}:
            raise RuntimeError("collaboration fanout 冻结 payload 字段损坏")
        if (payload["session_id"], payload["ledger_revision"], payload["event_hash"]) != (record.session_id, record.ledger_revision, record.event_hash):
            raise RuntimeError("collaboration fanout 冻结 owner/revision/hash 不一致")
        if record.ledger_revision < 1 or hashlib.sha256(_json(payload["event"]).encode()).hexdigest() != record.event_hash:
            raise RuntimeError("collaboration fanout 冻结 event hash 不一致")
        recipients = tuple(item.thread_id for item in record.recipients)
        if not recipients or recipients != tuple(sorted(set(recipients))):
            raise RuntimeError("collaboration fanout 冻结 recipient 集合损坏")
        for thread_id in recipients:
            validate_thread_id(thread_id)
        wakeup = None if record.wakeup_json is None else json.loads(record.wakeup_json)
        if wakeup is not None:
            CollaborationFanoutMixin._validate_wakeup(wakeup, recipients)
        digest = hashlib.sha256(_json({"payload": record.payload_json, "recipients": recipients, "wakeup": wakeup}).encode()).hexdigest()
        if (lease.operation_kind, lease.operation_identity, lease.preimage_hash, lease.recovery_ref) != (
            "collaboration_fanout", f"collaboration:{record.ledger_revision}", digest, f"collaboration:{record.ledger_revision}",
        ):
            raise RuntimeError("collaboration fanout 与原 operation lease preimage 不一致")
        expected_states = {"active", "settling"} if record.state == "pending" else {record.state}
        if lease.state not in expected_states:
            raise RuntimeError("collaboration fanout 与原 operation lease terminal 不一致")
        if record.state != "pending" and any(item.state == "pending" for item in record.recipients):
            raise RuntimeError("collaboration fanout 提前终结但仍有 pending recipient")
        if record.state == "completed" and wakeup is not None and record.wakeup_receipt_json is None:
            raise RuntimeError("collaboration fanout 提前完成但缺少 wakeup receipt")

    def list_collaboration_fanouts(self, *, pending_only: bool = False) -> tuple[CollaborationFanoutRecord, ...]:
        self._ensure_open()
        return tuple(self.get_collaboration_fanout(int(row[0])) for row in self._connection.execute(
            "SELECT ledger_revision FROM collaboration_fanouts" + (" WHERE state='pending'" if pending_only else "") + " ORDER BY ledger_revision",
        ))

    def latest_collaboration_fanouts(self) -> tuple[CollaborationFanoutRecord, ...]:
        self._ensure_open()
        return tuple(self.get_collaboration_fanout(int(row[0])) for row in self._connection.execute(
            "SELECT DISTINCT max_revision FROM (SELECT MAX(ledger_revision) AS max_revision "
            "FROM collaboration_deliveries GROUP BY recipient_thread_id) ORDER BY max_revision",
        ))

    def _require_collaboration_token(self, connection: sqlite3.Connection, revision: int, token: int):
        record = self.get_collaboration_fanout(revision)
        lease = self.get_lease(record.lease_id)
        if record.state != "pending" or lease.state not in {"active", "settling"} or lease.fencing_token != token:
            raise RuntimeError("collaboration callback 的原 lease/token 已失效")
        return record, lease

    def finish_collaboration_delivery(self, revision: int, thread_id: str, *, fencing_token: int, outcome: str, error: str | None = None) -> None:
        if outcome not in {"delivered", "recipient_deleted", "fence_closed"}:
            raise ValueError(f"非法 collaboration delivery outcome: {outcome}")
        with self._write_transaction() as connection:
            _record, lease = self._require_collaboration_token(connection, revision, fencing_token)
            if outcome == "delivered" and self.get_fence() != ("active", lease.captured_lifecycle_generation):
                raise RuntimeError("collaboration delivery 的原 lifecycle generation 已失效")
            changed = connection.execute("UPDATE collaboration_deliveries SET state=?,error=? "
                                         "WHERE ledger_revision=? AND recipient_thread_id=? AND state='pending'",
                                         (outcome, error, revision, thread_id)).rowcount
            if changed != 1:
                raise RuntimeError("collaboration delivery 已终结或 recipient 不在冻结集合")

    def record_collaboration_wakeup_receipt(self, revision: int, *, fencing_token: int, receipt_json: str) -> None:
        with self._write_transaction() as connection:
            record, lease = self._require_collaboration_token(connection, revision, fencing_token)
            if record.wakeup_json is None or self.get_fence() != ("active", lease.captured_lifecycle_generation):
                raise RuntimeError("collaboration wakeup 无原描述或 lifecycle 已失效")
            receipt = _json(json.loads(receipt_json))
            if record.wakeup_receipt_json is not None and record.wakeup_receipt_json != receipt:
                raise RuntimeError("collaboration wakeup receipt 冲突")
            connection.execute("UPDATE collaboration_fanouts SET wakeup_receipt_json=? WHERE ledger_revision=?", (receipt, revision))

    def finish_collaboration_fanout(self, revision: int, *, fencing_token: int, cancelled: bool = False) -> None:
        with self._write_transaction() as connection:
            record, lease = self._require_collaboration_token(connection, revision, fencing_token)
            if any(item.state == "pending" for item in record.recipients):
                raise RuntimeError("collaboration fanout 仍有未终结 recipient")
            if not cancelled and record.wakeup_json is not None and record.wakeup_receipt_json is None:
                raise RuntimeError("collaboration fanout 的显式 wakeup 尚未确认")
            connection.execute("UPDATE collaboration_fanouts SET state=? WHERE ledger_revision=?", ("cancelled" if cancelled else "completed", revision))
            if lease.state == "active":
                lease = self.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=fencing_token,
                                                transaction_connection=connection)
            self.settle_lease(lease_id=lease.lease_id, expected_fencing_token=fencing_token,
                              outcome="cancelled" if cancelled else "completed", transaction_connection=connection)

    def collaboration_recipient_pending(self, thread_id: str, through_revision: int) -> bool:
        self._ensure_open()
        return self._connection.execute(
            "SELECT 1 FROM collaboration_deliveries WHERE recipient_thread_id=? "
            "AND ledger_revision<=? AND state='pending' LIMIT 1", (thread_id, through_revision),
        ).fetchone() is not None

    def record_collaboration_delivery_failure(self, revision: int, thread_id: str, *, fencing_token: int, error: str) -> None:
        with self._write_transaction() as connection:
            self._require_collaboration_token(connection, revision, fencing_token)
            updated = connection.execute("UPDATE collaboration_deliveries SET error=? WHERE ledger_revision=? "
                                         "AND recipient_thread_id=? AND state='pending'", (error, revision, thread_id)).rowcount
            if updated != 1:
                raise RuntimeError("不能覆盖已终结的 collaboration delivery")
