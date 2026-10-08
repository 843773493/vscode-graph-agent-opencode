"""通信冷观察的持久 owner：冻结请求、唯一页和有界恢复期限。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_control_operation_lease.operation_lease import (
    delete_terminal_leases,
)

OBSERVATION_ERROR_CODES = frozenset({
    "read-snapshot-invalid", "read-snapshot-mismatch", "read-snapshot-expired",
    "operation-retry-expired", "target-deleted",
})


class RemoteObservationError(ValueError):
    """跨 API/Gateway/工具使用同一不含内部解析细节的错误闭集。"""

    def __init__(self, code: str) -> None:
        if code not in OBSERVATION_ERROR_CODES:
            raise ValueError(f"未知 observation 错误码: {code}")
        self.code = code
        super().__init__(code)

    def to_wire(self) -> dict[str, object]:
        return {"code": self.code, "message": self.code, "detail": {}}


def observation_error_code(value: object) -> str | None:
    """只接受闭集 code，不转发远端错误 message/detail。"""
    if isinstance(value, str):
        return value if value in OBSERVATION_ERROR_CODES else None
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and code in OBSERVATION_ERROR_CODES:
            return code
        return observation_error_code(value.get("detail"))
    return None


REMOTE_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS remote_observations (
    observation_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('source','target')),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    snapshot_json TEXT CHECK(snapshot_json IS NULL OR json_valid(snapshot_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(observation_id,side)
)
"""
REMOTE_OBSERVATION_PAGES_DDL = """
CREATE TABLE IF NOT EXISTS remote_observation_pages (
    observation_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('source','target')),
    page_identity TEXT NOT NULL,
    page_ordinal INTEGER NOT NULL CHECK(page_ordinal >= 0),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    target_result_json TEXT CHECK(target_result_json IS NULL OR json_valid(target_result_json)),
    PRIMARY KEY(observation_id,side,page_ordinal),
    UNIQUE(side,page_identity),
    FOREIGN KEY(observation_id,side) REFERENCES remote_observations(observation_id,side)
        ON DELETE CASCADE
)
"""
REMOTE_OBSERVATION_EXPIRY_DDL = """
CREATE INDEX IF NOT EXISTS idx_remote_observations_expiry ON remote_observations(expires_at)
"""
REMOTE_OBSERVATION_KEY_DDL = """
CREATE TABLE IF NOT EXISTS remote_observation_key (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    cursor_key BLOB NOT NULL CHECK(length(cursor_key)=32)
)
"""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RemoteObservation:
    observation_id: str
    side: str
    request: dict[str, object]
    snapshot: dict[str, object] | None
    result: dict[str, object] | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class RemoteObservationPage:
    observation_id: str
    side: str
    page_identity: str
    page_ordinal: int
    request: dict[str, object]
    result: dict[str, object] | None
    target_result: dict[str, object] | None


def _record(row: sqlite3.Row) -> RemoteObservation:
    return RemoteObservation(
        row["observation_id"], row["side"], json.loads(row["request_json"]),
        None if row["snapshot_json"] is None else json.loads(row["snapshot_json"]),
        None if row["result_json"] is None else json.loads(row["result_json"]),
        row["expires_at"],
    )


def _page(row: sqlite3.Row) -> RemoteObservationPage:
    return RemoteObservationPage(
        row["observation_id"], row["side"], row["page_identity"], row["page_ordinal"],
        json.loads(row["request_json"]),
        None if row["result_json"] is None else json.loads(row["result_json"]),
        None if row["target_result_json"] is None else json.loads(row["target_result_json"]),
    )


class CommunicationObservationMixin:
    def list_remote_observations(self) -> tuple[RemoteObservation, ...]:
        self._ensure_open()
        return tuple(_record(row) for row in self._connection.execute(
            "SELECT * FROM remote_observations ORDER BY expires_at,observation_id,side"
        ))

    def list_remote_pages(self, observation_id: str, *, side: str) -> tuple[RemoteObservationPage, ...]:
        self._ensure_open()
        return tuple(_page(row) for row in self._connection.execute(
            "SELECT * FROM remote_observation_pages WHERE observation_id=? AND side=? ORDER BY page_ordinal",
            (observation_id, side),
        ))

    def get_remote_page(self, observation_id: str, *, side: str, page_identity: str) -> RemoteObservationPage:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM remote_observation_pages WHERE observation_id=? AND side=? AND page_identity=?",
            (observation_id, side, page_identity),
        ).fetchone()
        if row is None:
            raise RemoteObservationError("operation-retry-expired")
        return _page(row)

    def observation_cursor_key(self, *, create: bool = False) -> bytes:
        self._ensure_open()
        if create:
            with self._write_transaction() as connection:
                connection.execute("INSERT INTO remote_observation_key VALUES(1,?) ON CONFLICT DO NOTHING", (secrets.token_bytes(32),))
        row = self._connection.execute("SELECT cursor_key FROM remote_observation_key WHERE singleton_id=1").fetchone()
        if row is None:
            raise RemoteObservationError("read-snapshot-expired")
        return bytes(row[0])

    def admit_remote_page(self, observation_id: str, *, side: str, page_identity: str, page_ordinal: int, request: dict[str, object]) -> dict[str, object] | None:
        submitted = canonical_json(request)
        with self._write_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_observation_pages WHERE side=? AND "
                "(page_identity=? OR (observation_id=? AND page_ordinal=?))",
                (side, page_identity, observation_id, page_ordinal),
            ).fetchall()
            if rows:
                if len(rows) != 1 or (rows[0]["observation_id"], rows[0]["page_identity"], rows[0]["page_ordinal"], rows[0]["request_json"]) != (observation_id, page_identity, page_ordinal, submitted):
                    raise RemoteObservationError("read-snapshot-mismatch")
                return _page(rows[0]).result
            connection.execute(
                "INSERT INTO remote_observation_pages VALUES(?,?,?,?,?,NULL,NULL)",
                (observation_id, side, page_identity, page_ordinal, submitted),
            )
        return None

    def commit_remote_page(self, observation_id: str, *, side: str, page_identity: str, result: dict[str, object], target_result: dict[str, object] | None = None) -> None:
        submitted = canonical_json(result)
        with self._write_transaction() as connection:
            row = connection.execute("SELECT result_json,target_result_json FROM remote_observation_pages WHERE observation_id=? AND side=? AND page_identity=?", (observation_id, side, page_identity)).fetchone()
            if row is None:
                raise RemoteObservationError("operation-retry-expired")
            target_json = canonical_json(target_result) if target_result is not None else row[1]
            if (row[0] is not None and row[0] != submitted) or (row[1] is not None and row[1] != target_json):
                raise RemoteObservationError("read-snapshot-mismatch")
            connection.execute("UPDATE remote_observation_pages SET result_json=?,target_result_json=? WHERE observation_id=? AND side=? AND page_identity=?", (submitted, target_json, observation_id, side, page_identity))

    def get_remote_observation(self, observation_id: str, *, side: str) -> RemoteObservation | None:
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM remote_observations WHERE observation_id=? AND side=?", (observation_id, side)).fetchone()
        return None if row is None else _record(row)

    def admit_remote_observation(self, observation_id: str, *, side: str, request: dict[str, object], expires_at: str) -> RemoteObservation:
        submitted = canonical_json(request)
        expires_at = datetime.fromisoformat(expires_at).astimezone(UTC).isoformat()
        now = datetime.now(UTC).isoformat()
        with self._write_transaction() as connection:
            row = connection.execute("SELECT * FROM remote_observations WHERE observation_id=? AND side=?", (observation_id, side)).fetchone()
            if row is not None:
                if row["request_json"] != submitted or row["expires_at"] != expires_at:
                    raise RemoteObservationError("read-snapshot-mismatch")
                return _record(row)
            connection.execute("INSERT INTO remote_observations VALUES(?,?,?,NULL,NULL,?,?,?)", (observation_id, side, submitted, expires_at, now, now))
        return self.get_remote_observation(observation_id, side=side)

    def commit_remote_observation(self, observation_id: str, *, side: str, snapshot: dict[str, object] | None = None, result: dict[str, object] | None = None) -> RemoteObservation:
        with self._write_transaction() as connection:
            row = connection.execute("SELECT * FROM remote_observations WHERE observation_id=? AND side=?", (observation_id, side)).fetchone()
            if row is None:
                raise RemoteObservationError("operation-retry-expired")
            values = {}
            for name, value in (("snapshot_json", snapshot), ("result_json", result)):
                submitted = canonical_json(value) if value is not None else row[name]
                if row[name] is not None and row[name] != submitted:
                    raise RemoteObservationError("read-snapshot-mismatch")
                values[name] = submitted
            connection.execute("UPDATE remote_observations SET snapshot_json=?,result_json=?,updated_at=? WHERE observation_id=? AND side=?", (values["snapshot_json"], values["result_json"], datetime.now(UTC).isoformat(), observation_id, side))
        return self.get_remote_observation(observation_id, side=side)

    def delete_remote_observation(self, observation_id: str, *, side: str) -> None:
        """同一写事务清理原 observation、级联页和全部关联终态 lease。"""
        with self._write_transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM remote_observations WHERE observation_id=? AND side=?",
                (observation_id, side),
            ).fetchone() is None:
                return
            pages = connection.execute(
                "SELECT page_identity FROM remote_observation_pages WHERE observation_id=? AND side=?",
                (observation_id, side),
            ).fetchall()
            identities = (side + "-observation|" + observation_id, *(side + "-page|" + row["page_identity"] for row in pages))
            delete_terminal_leases(connection, identities, operation_kind="remote_observation")
            connection.execute("DELETE FROM remote_observations WHERE observation_id=? AND side=?", (observation_id, side))
