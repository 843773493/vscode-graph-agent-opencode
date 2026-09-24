"""hub/target 的持久 first-use replay registry、channel 注册表与 route hint。

这些表位于 Gateway control-plane 的独立 SQLite 库（``state/gateway/federation/
control.sqlite``），不写入工作区业务库，也不与 ``gateway.sqlite`` 竞争进程所有
权锁；hub 只保存 peer/policy/replay/route 等控制面状态与瞬时 in-flight
correlation，不保存业务消息正文。

replay registry 在提交 ``(issuer, origin, audience, grant_kind, nonce)`` 及
grant/request/path hash 后才允许 lookup 或转发；重复或同 nonce 不同 preimage
一律拒绝。record 保留到 ``expiry + clock skew + transport replay margin`` 越过，
Gateway 重启不清空有效窗口。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sqlite_state import SQLiteStateDatabase, utc_now_text
from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_EPOCH_STALE,
    FEDERATION_GRANT_REPLAY,
    FEDERATION_GRANT_REPLAY_CONFLICT,
    FederationError,
)
from app.gateway.federation.grants import GRANT_CLOCK_SKEW_SECONDS

# transport 重放余量：网络重试可能把已在途的 grant 送达得更晚。
TRANSPORT_REPLAY_MARGIN_SECONDS = 30.0
# route hint 短 TTL：hint 不是业务事实或授权，target 每次操作仍重新验证。
ROUTE_HINT_TTL_SECONDS = 30.0

FEDERATION_SCHEMA_VERSION = 1
FEDERATION_MIGRATIONS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS federation_replay_registry (
    issuer_gateway_id TEXT NOT NULL,
    origin_gateway_id TEXT NOT NULL,
    audience_gateway_id TEXT NOT NULL,
    grant_kind TEXT NOT NULL,
    nonce TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (
        issuer_gateway_id, origin_gateway_id, audience_gateway_id,
        grant_kind, nonce
    )
);
CREATE TABLE IF NOT EXISTS federation_channel_registry (
    channel_instance_id TEXT PRIMARY KEY,
    peer_gateway_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    channel_epoch INTEGER NOT NULL,
    direction TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_route_hint (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    resolved_main_thread_id TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, session_id)
);
""",
)


def federation_control_database(*, gateway_root: Path) -> SQLiteStateDatabase:
    """联邦控制面膜库：独立文件 + WAL，允许 Gateway handoff 期间两进程共存。"""

    return SQLiteStateDatabase(
        path=gateway_root / "federation" / "control.sqlite",
        schema_version=FEDERATION_SCHEMA_VERSION,
        migrations=FEDERATION_MIGRATIONS,
        allow_shared_processes=True,
    )


def _preimage_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    issuer_gateway_id: str
    origin_gateway_id: str
    audience_gateway_id: str
    grant_kind: str
    nonce: str
    preimage_hash: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class FederationChannelRecord:
    channel_instance_id: str
    peer_gateway_id: str
    connection_id: str
    channel_epoch: int
    direction: str
    connected_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class RouteHint:
    gateway_id: str
    workspace_id: str
    session_id: str
    resolved_main_thread_id: str
    catalog_revision: str
    expires_at: datetime


class FederationControlStore:
    """联邦控制面唯一持久入口：replay registry、channel 注册表与 route hint。"""

    def __init__(self, *, database: SQLiteStateDatabase) -> None:
        self._database = database

    def close(self) -> None:
        self._database.close()

    def _connection(self):
        return self._database.connection()

    # --- replay registry -------------------------------------------------

    def adopt_grant(
        self,
        *,
        issuer_gateway_id: str,
        origin_gateway_id: str,
        audience_gateway_id: str,
        grant_kind: str,
        nonce: str,
        preimage: dict[str, object],
        expires_at: datetime,
    ) -> None:
        """提交一次 first-use；重复或同 nonce 不同 preimage 显式拒绝。"""

        digest = _preimage_hash(preimage)
        retention = expires_at + timedelta(
            seconds=GRANT_CLOCK_SKEW_SECONDS + TRANSPORT_REPLAY_MARGIN_SECONDS
        )
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT preimage_hash FROM federation_replay_registry
                WHERE issuer_gateway_id = ? AND origin_gateway_id = ?
                  AND audience_gateway_id = ? AND grant_kind = ? AND nonce = ?
                """,
                (
                    issuer_gateway_id,
                    origin_gateway_id,
                    audience_gateway_id,
                    grant_kind,
                    nonce,
                ),
            ).fetchone()
            if row is not None:
                if str(row["preimage_hash"]) != digest:
                    raise FederationError(
                        FEDERATION_GRANT_REPLAY_CONFLICT,
                        "同 nonce 提交了不同 preimage，拒绝重放",
                        detail={"nonce": nonce, "grant_kind": grant_kind},
                    )
                raise FederationError(
                    FEDERATION_GRANT_REPLAY,
                    "grant nonce 已被使用，拒绝重放",
                    detail={"nonce": nonce, "grant_kind": grant_kind},
                )
            connection.execute(
                """
                INSERT INTO federation_replay_registry (
                    issuer_gateway_id, origin_gateway_id, audience_gateway_id,
                    grant_kind, nonce, preimage_hash, expires_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issuer_gateway_id,
                    origin_gateway_id,
                    audience_gateway_id,
                    grant_kind,
                    nonce,
                    digest,
                    retention.isoformat(),
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).isoformat()
        connection = self._connection()
        try:
            cursor = connection.execute(
                "DELETE FROM federation_replay_registry WHERE expires_at <= ?",
                (current,),
            )
            return int(cursor.rowcount or 0)
        finally:
            connection.close()

    def replay_entries(self) -> tuple[ReplayEntry, ...]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM federation_replay_registry ORDER BY recorded_at"
            ).fetchall()
            return tuple(
                ReplayEntry(
                    issuer_gateway_id=str(row["issuer_gateway_id"]),
                    origin_gateway_id=str(row["origin_gateway_id"]),
                    audience_gateway_id=str(row["audience_gateway_id"]),
                    grant_kind=str(row["grant_kind"]),
                    nonce=str(row["nonce"]),
                    preimage_hash=str(row["preimage_hash"]),
                    expires_at=datetime.fromisoformat(str(row["expires_at"])),
                )
                for row in rows
            )
        finally:
            connection.close()

    # --- channel registry -------------------------------------------------

    def register_channel(
        self,
        *,
        channel_instance_id: str,
        peer_gateway_id: str,
        connection_id: str,
        channel_epoch: int,
        direction: str,
    ) -> FederationChannelRecord:
        if channel_epoch < 1:
            raise FederationError(
                FEDERATION_CHANNEL_EPOCH_STALE, "channel epoch 必须为正"
            )
        now = datetime.now(UTC)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT channel_epoch FROM federation_channel_registry
                WHERE connection_id = ? ORDER BY channel_epoch DESC LIMIT 1
                """,
                (connection_id,),
            ).fetchone()
            if previous is not None and int(previous["channel_epoch"]) >= channel_epoch:
                raise FederationError(
                    FEDERATION_CHANNEL_EPOCH_STALE,
                    "channel epoch 未超过已登记版本，拒绝旧 channel 抢占",
                    detail={
                        "registered_epoch": int(previous["channel_epoch"]),
                        "offered_epoch": channel_epoch,
                    },
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO federation_channel_registry (
                    channel_instance_id, peer_gateway_id, connection_id,
                    channel_epoch, direction, connected_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_instance_id,
                    peer_gateway_id,
                    connection_id,
                    channel_epoch,
                    direction,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FederationChannelRecord(
            channel_instance_id=channel_instance_id,
            peer_gateway_id=peer_gateway_id,
            connection_id=connection_id,
            channel_epoch=channel_epoch,
            direction=direction,
            connected_at=now,
            last_seen_at=now,
        )

    def touch_channel(self, *, channel_instance_id: str) -> None:
        connection = self._connection()
        try:
            connection.execute(
                "UPDATE federation_channel_registry SET last_seen_at = ? "
                "WHERE channel_instance_id = ?",
                (utc_now_text(), channel_instance_id),
            )
        finally:
            connection.close()

    def unregister_channel(self, *, channel_instance_id: str) -> None:
        connection = self._connection()
        try:
            connection.execute(
                "DELETE FROM federation_channel_registry WHERE channel_instance_id = ?",
                (channel_instance_id,),
            )
        finally:
            connection.close()

    def active_channels(self) -> tuple[FederationChannelRecord, ...]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM federation_channel_registry ORDER BY connected_at"
            ).fetchall()
            return tuple(
                FederationChannelRecord(
                    channel_instance_id=str(row["channel_instance_id"]),
                    peer_gateway_id=str(row["peer_gateway_id"]),
                    connection_id=str(row["connection_id"]),
                    channel_epoch=int(row["channel_epoch"]),
                    direction=str(row["direction"]),
                    connected_at=datetime.fromisoformat(str(row["connected_at"])),
                    last_seen_at=datetime.fromisoformat(str(row["last_seen_at"])),
                )
                for row in rows
            )
        finally:
            connection.close()

    def channels_for_connection(self, connection_id: str) -> tuple[str, ...]:
        """返回该连接当前 active 的 channel 实例 ID（重连只需刷新 route）。"""

        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT channel_instance_id FROM federation_channel_registry "
                "WHERE connection_id = ? ORDER BY channel_epoch",
                (connection_id,),
            ).fetchall()
            return tuple(str(row["channel_instance_id"]) for row in rows)
        finally:
            connection.close()

    # --- route hint -------------------------------------------------------

    def record_route_hint(
        self,
        *,
        gateway_id: str,
        workspace_id: str,
        session_id: str,
        resolved_main_thread_id: str,
        catalog_revision: str,
        now: datetime | None = None,
    ) -> RouteHint:
        current = now or datetime.now(UTC)
        expires_at = current + timedelta(seconds=ROUTE_HINT_TTL_SECONDS)
        connection = self._connection()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO federation_route_hint ("
                "gateway_id, workspace_id, session_id, resolved_main_thread_id, "
                "catalog_revision, expires_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    gateway_id,
                    workspace_id,
                    session_id,
                    resolved_main_thread_id,
                    catalog_revision,
                    expires_at.isoformat(),
                    current.isoformat(),
                ),
            )
        finally:
            connection.close()
        return RouteHint(
            gateway_id=gateway_id,
            workspace_id=workspace_id,
            session_id=session_id,
            resolved_main_thread_id=resolved_main_thread_id,
            catalog_revision=catalog_revision,
            expires_at=expires_at,
        )

    def read_route_hint(
        self,
        *,
        gateway_id: str,
        workspace_id: str,
        session_id: str,
        now: datetime | None = None,
    ) -> RouteHint | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT * FROM federation_route_hint WHERE gateway_id = ? "
                "AND workspace_id = ? AND session_id = ?",
                (gateway_id, workspace_id, session_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= (now or datetime.now(UTC)):
            return None
        return RouteHint(
            gateway_id=str(row["gateway_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            resolved_main_thread_id=str(row["resolved_main_thread_id"]),
            catalog_revision=str(row["catalog_revision"]),
            expires_at=expires_at,
        )


__all__ = [
    "FEDERATION_MIGRATIONS",
    "FEDERATION_SCHEMA_VERSION",
    "ROUTE_HINT_TTL_SECONDS",
    "TRANSPORT_REPLAY_MARGIN_SECONDS",
    "FederationChannelRecord",
    "FederationControlStore",
    "ReplayEntry",
    "RouteHint",
    "federation_control_database",
]
