from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.gateway.control.gateway_state import GatewayStateStore

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
GUEST_COOKIE_PREFIX = "guest:"
USER_ACCESS_COOKIE_NAME = "boxteam-user-access"


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    display_name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class LeaseSummary:
    occupied: bool
    access_session_id: str | None
    lease_generation: int | None
    client_label: str | None
    heartbeat_at: str | None
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class UserListRecord:
    user: UserRecord
    lease: LeaseSummary


@dataclass(frozen=True, slots=True)
class UserAccessContext:
    kind: str
    user_id: str | None
    access_session_id: str
    lease_generation: int
    invalidated: asyncio.Event


class UserLeaseOccupiedError(RuntimeError):
    def __init__(self, summary: LeaseSummary) -> None:
        super().__init__("用户 ID 当前已被另一台电脑占用")
        self.summary = summary


class UserAccessService:
    def __init__(
        self,
        *,
        state: GatewayStateStore,
        lease_ttl_seconds: int = 45,
        guest_ttl_days: int = 7,
    ) -> None:
        if lease_ttl_seconds < 10:
            raise ValueError("用户访问租约 TTL 不能小于 10 秒")
        if guest_ttl_days < 1:
            raise ValueError("游客追踪 TTL 必须大于 0 天")
        self._state = state
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds)
        self._guest_ttl = timedelta(days=guest_ttl_days)
        self._invalidations: dict[str, asyncio.Event] = {}

    @staticmethod
    def _validate_user_id(user_id: str) -> str:
        if not USER_ID_PATTERN.fullmatch(user_id):
            raise ValueError(
                "user_id 只能包含 ASCII 字母、数字、下划线和短横线，长度为 1-64"
            )
        return user_id

    @staticmethod
    def _validate_display_name(display_name: str) -> str:
        value = display_name.strip()
        if not value or len(value) > 120:
            raise ValueError("用户显示名称不能为空且不能超过 120 个字符")
        return value

    @staticmethod
    def _new_user_id() -> str:
        return f"user-{secrets.token_urlsafe(8)}".replace("_", "-")

    @staticmethod
    def _new_session_id(prefix: str = "access") -> str:
        return f"{prefix}-{secrets.token_urlsafe(24)}"

    def _connection(self) -> sqlite3.Connection:
        return self._state.connection()

    @staticmethod
    def _lease_from_row(row: sqlite3.Row | None, *, now: datetime) -> LeaseSummary:
        if row is None:
            return LeaseSummary(False, None, None, None, None, None)
        expires_at = str(row[6])
        if _parse_timestamp(expires_at) <= now:
            return LeaseSummary(False, None, None, None, None, None)
        return LeaseSummary(
            occupied=True,
            access_session_id=str(row[1]),
            lease_generation=int(row[0]),
            client_label=str(row[2]) if row[2] is not None else None,
            heartbeat_at=str(row[4]),
            expires_at=expires_at,
        )

    def _read_lease(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> LeaseSummary:
        row = connection.execute(
            """
            SELECT lease_generation, access_session_id, client_label,
                   acquired_at, heartbeat_at, expires_at, expires_at
            FROM user_access_lease
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return self._lease_from_row(row, now=now or _now())

    def _clear_expired_lease(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        *,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT access_session_id, expires_at FROM user_access_lease WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is not None and _parse_timestamp(str(row[1])) <= now:
            connection.execute("DELETE FROM user_access_lease WHERE user_id = ?", (user_id,))
            event = self._invalidations.pop(str(row[0]), None)
            if event is not None:
                event.set()

    def list_users(self) -> tuple[UserListRecord, ...]:
        connection = self._connection()
        try:
            now = _now()
            rows = connection.execute(
                """
                SELECT user_id, display_name, created_at
                FROM user_account
                WHERE deleted_at IS NULL
                ORDER BY created_at ASC, user_id ASC
                """
            ).fetchall()
            records: list[UserListRecord] = []
            for row in rows:
                user_id = str(row[0])
                self._clear_expired_lease(connection, user_id, now=now)
                records.append(
                    UserListRecord(
                        user=UserRecord(user_id, str(row[1]), str(row[2])),
                        lease=self._read_lease(connection, user_id, now=now),
                    )
                )
            return tuple(records)
        finally:
            connection.close()

    def create_user(self, *, display_name: str, user_id: str | None = None) -> UserRecord:
        normalized_name = self._validate_display_name(display_name)
        normalized_id = self._validate_user_id(user_id) if user_id else self._new_user_id()
        created_at = _timestamp(_now())
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO user_account(user_id, display_name, created_at) VALUES (?, ?, ?)",
                    (normalized_id, normalized_name, created_at),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ValueError(f"用户 ID 已存在: {normalized_id}") from error
            return UserRecord(normalized_id, normalized_name, created_at)
        finally:
            connection.close()

    def delete_user(self, user_id: str) -> None:
        self._validate_user_id(user_id)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = self._read_lease(connection, user_id)
                if lease.occupied:
                    raise UserLeaseOccupiedError(lease)
                cursor = connection.execute(
                    "DELETE FROM user_account WHERE user_id = ? AND deleted_at IS NULL",
                    (user_id,),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"未知用户 ID: {user_id}")
                connection.execute("COMMIT")
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _user_exists(self, connection: sqlite3.Connection, user_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM user_account WHERE user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return row is not None

    def acquire_user(
        self,
        *,
        user_id: str,
        client_label: str | None,
        takeover: bool = False,
    ) -> UserAccessContext:
        self._validate_user_id(user_id)
        normalized_label = client_label.strip()[:120] if client_label else None
        now = _now()
        expires_at = now + self._lease_ttl
        session_id = self._new_session_id()
        connection = self._connection()
        invalidated_session_id: str | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not self._user_exists(connection, user_id):
                    raise KeyError(f"未知用户 ID: {user_id}")
                existing_row = connection.execute(
                    """
                    SELECT lease_generation, access_session_id, client_label,
                           acquired_at, heartbeat_at, expires_at, expires_at
                    FROM user_access_lease
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                existing = self._lease_from_row(existing_row, now=now)
                if existing.occupied and not takeover:
                    raise UserLeaseOccupiedError(existing)
                if existing_row is not None:
                    invalidated_session_id = str(existing_row[1])
                    generation = int(existing_row[0]) + 1
                    connection.execute("DELETE FROM user_access_lease WHERE user_id = ?", (user_id,))
                else:
                    generation = 1
                connection.execute(
                    """
                    INSERT INTO user_access_lease(
                        user_id, lease_generation, access_session_id, client_label,
                        acquired_at, heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        generation,
                        session_id,
                        normalized_label,
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(expires_at),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()
        if invalidated_session_id is not None:
            self._invalidations.setdefault(invalidated_session_id, asyncio.Event()).set()
        event = asyncio.Event()
        self._invalidations[session_id] = event
        return UserAccessContext("user", user_id, session_id, generation, event)

    def acquire_guest(self, *, tracking: dict[str, object] | None = None) -> UserAccessContext:
        guest_id = f"guest-{secrets.token_urlsafe(16)}"
        session_id = self._new_session_id("guest")
        now = _now()
        expires_at = now + self._guest_ttl
        payload = dict(tracking or {})
        payload["access_session_id"] = session_id
        payload["guest_id"] = guest_id
        connection = self._connection()
        try:
            connection.execute(
                """
                INSERT INTO guest_tracking(
                    guest_id, tracking_json, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    guest_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(expires_at),
                ),
            )
        finally:
            connection.close()
        cookie_value = f"{GUEST_COOKIE_PREFIX}{guest_id}:{session_id}"
        event = asyncio.Event()
        self._invalidations[session_id] = event
        return UserAccessContext("guest", None, cookie_value, 1, event)

    def resolve_cookie(self, cookie_value: str | None) -> UserAccessContext | None:
        if not cookie_value:
            return None
        if cookie_value.startswith(GUEST_COOKIE_PREFIX):
            parts = cookie_value.split(":", 2)
            if len(parts) != 3:
                return None
            _, guest_id, session_id = parts
            connection = self._connection()
            try:
                row = connection.execute(
                    "SELECT tracking_json, expires_at FROM guest_tracking WHERE guest_id = ?",
                    (guest_id,),
                ).fetchone()
                if row is None:
                    return None
                if _parse_timestamp(str(row[1])) <= _now():
                    event = self._invalidations.get(session_id)
                    if event is not None:
                        event.set()
                    return None
                tracking = json.loads(str(row[0]))
                if not isinstance(tracking, dict) or tracking.get("access_session_id") != session_id:
                    return None
                connection.execute(
                    "UPDATE guest_tracking SET last_seen_at = ? WHERE guest_id = ?",
                    (_timestamp(_now()), guest_id),
                )
            finally:
                connection.close()
            event = self._invalidations.setdefault(session_id, asyncio.Event())
            return UserAccessContext("guest", None, cookie_value, 1, event)
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT user_id, lease_generation, access_session_id, expires_at
                FROM user_access_lease
                WHERE access_session_id = ?
                """,
                (cookie_value,),
            ).fetchone()
            if row is None:
                return None
            if _parse_timestamp(str(row[3])) <= _now():
                event = self._invalidations.get(cookie_value)
                if event is not None:
                    event.set()
                return None
            event = self._invalidations.setdefault(cookie_value, asyncio.Event())
            return UserAccessContext(
                "user", str(row[0]), cookie_value, int(row[1]), event
            )
        finally:
            connection.close()

    def expires_at(self, context: UserAccessContext) -> str | None:
        connection = self._connection()
        try:
            if context.kind == "guest":
                parts = context.access_session_id.split(":", 2)
                if len(parts) != 3:
                    return None
                row = connection.execute(
                    "SELECT expires_at FROM guest_tracking WHERE guest_id = ?",
                    (parts[1],),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT expires_at FROM user_access_lease WHERE access_session_id = ?",
                    (context.access_session_id,),
                ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            connection.close()

    def heartbeat(self, context: UserAccessContext) -> UserAccessContext:
        if context.kind != "user" or context.user_id is None:
            return context
        now = _now()
        connection = self._connection()
        try:
            cursor = connection.execute(
                """
                UPDATE user_access_lease
                SET heartbeat_at = ?, expires_at = ?
                WHERE user_id = ? AND access_session_id = ? AND lease_generation = ?
                """,
                (
                    _timestamp(now),
                    _timestamp(now + self._lease_ttl),
                    context.user_id,
                    context.access_session_id,
                    context.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                context.invalidated.set()
                raise PermissionError("user_session_taken_over")
        finally:
            connection.close()
        return context

    def release(self, context: UserAccessContext) -> None:
        if context.kind == "guest":
            parts = context.access_session_id.split(":", 2)
            if len(parts) == 3:
                connection = self._connection()
                try:
                    connection.execute("DELETE FROM guest_tracking WHERE guest_id = ?", (parts[1],))
                finally:
                    connection.close()
            context.invalidated.set()
            self._invalidations.pop(context.access_session_id.split(":")[-1], None)
            return
        if context.user_id is None:
            return
        connection = self._connection()
        try:
            connection.execute(
                "DELETE FROM user_access_lease WHERE user_id = ? AND access_session_id = ?",
                (context.user_id, context.access_session_id),
            )
        finally:
            connection.close()
        context.invalidated.set()
        self._invalidations.pop(context.access_session_id, None)

    def cleanup_expired(self) -> tuple[int, int]:
        now = _timestamp(_now())
        connection = self._connection()
        try:
            expired_leases = connection.execute(
                "SELECT access_session_id FROM user_access_lease WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            expired_guests = connection.execute(
                "SELECT guest_id, tracking_json FROM guest_tracking WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            connection.execute("DELETE FROM user_access_lease WHERE expires_at <= ?", (now,))
            connection.execute("DELETE FROM guest_tracking WHERE expires_at <= ?", (now,))
        finally:
            connection.close()
        for row in expired_leases:
            session_id = str(row[0])
            self._invalidations.setdefault(session_id, asyncio.Event()).set()
            self._invalidations.pop(session_id, None)
        for row in expired_guests:
            tracking = json.loads(str(row[1]))
            session_id = tracking.get("access_session_id") if isinstance(tracking, dict) else None
            if isinstance(session_id, str):
                self._invalidations.setdefault(session_id, asyncio.Event()).set()
                self._invalidations.pop(session_id, None)
        return len(expired_leases), len(expired_guests)
