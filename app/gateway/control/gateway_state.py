from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.core.sqlite_state import SQLiteDiagnostics, SQLiteStateDatabase, utc_now_text


_GATEWAY_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS gateway_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gateway_workspace_registry (
        workspace_id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_account (
        user_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS user_access_lease (
        user_id TEXT PRIMARY KEY REFERENCES user_account(user_id) ON DELETE CASCADE,
        lease_generation INTEGER NOT NULL,
        access_session_id TEXT NOT NULL UNIQUE,
        client_label TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_view_state (
        user_id TEXT NOT NULL REFERENCES user_account(user_id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_anchor TEXT,
        scroll_offset REAL NOT NULL DEFAULT 0,
        follow_latest INTEGER NOT NULL DEFAULT 0,
        projection_version INTEGER NOT NULL DEFAULT 1,
        tool_details_expanded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, workspace_id, session_id)
    );
    CREATE TABLE IF NOT EXISTS guest_tracking (
        guest_id TEXT PRIMARY KEY,
        tracking_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """,
)


@dataclass(frozen=True, slots=True)
class GatewayConfigRecord:
    config_key: str
    config_version: int
    payload: dict[str, object]


class GatewayStateStore:
    def __init__(self, *, path: Path) -> None:
        self._database = SQLiteStateDatabase(
            path=path,
            schema_version=len(_GATEWAY_MIGRATIONS),
            migrations=_GATEWAY_MIGRATIONS,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

    def connection(self) -> sqlite3.Connection:
        return self._database.connection()

    def set_config(
        self,
        *,
        config_key: str,
        config_version: int,
        payload: dict[str, object],
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                """
                INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    config_key,
                    config_version,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now_text(),
                ),
            )
        finally:
            connection.close()

    def get_config(self, config_key: str) -> GatewayConfigRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT config_key, config_version, payload_json FROM gateway_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[2]))
            if not isinstance(payload, dict):
                raise ValueError(f"Gateway SQLite 配置不是对象: key={config_key}")
            return GatewayConfigRecord(
                config_key=str(row[0]),
                config_version=int(row[1]),
                payload=payload,
            )
        finally:
            connection.close()

    def load_workspace_registry(self) -> dict[str, object] | None:
        meta = self.get_config("workspace_registry_meta")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM gateway_workspace_registry
                ORDER BY position ASC, workspace_id ASC
                """
            ).fetchall()
            if meta is None and not rows:
                return None
            targets: list[object] = []
            for row in rows:
                payload = json.loads(str(row[0]))
                if not isinstance(payload, dict):
                    raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                targets.append(payload)
            metadata = meta.payload if meta is not None else {}
            return {
                "schema_version": int(metadata.get("schema_version", 9)),
                "active_workspace_id": metadata.get("active_workspace_id"),
                "order_customized": bool(metadata.get("order_customized", False)),
                "remote_gateway_connections": metadata.get(
                    "remote_gateway_connections", []
                ),
                "targets": targets,
            }
        finally:
            connection.close()

    def replace_workspace_registry(self, payload: dict[str, object]) -> None:
        targets = payload.get("targets", [])
        remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(targets, list) or not isinstance(remote_connections, list):
            raise ValueError("Gateway SQLite 注册表 payload 结构无效")
        metadata = {
            "schema_version": int(payload.get("schema_version", 9)),
            "active_workspace_id": payload.get("active_workspace_id"),
            "order_customized": bool(payload.get("order_customized", False)),
            "remote_gateway_connections": remote_connections,
        }
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM gateway_workspace_registry")
                for position, target in enumerate(targets):
                    if not isinstance(target, dict):
                        raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                    workspace_id = target.get("workspace_id")
                    if not isinstance(workspace_id, str) or not workspace_id:
                        raise ValueError("Gateway SQLite 工作区注册记录缺少 workspace_id")
                    connection.execute(
                        """
                        INSERT INTO gateway_workspace_registry(
                            workspace_id, position, active, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id,
                            position,
                            int(workspace_id == metadata["active_workspace_id"]),
                            json.dumps(target, ensure_ascii=False, sort_keys=True),
                            utc_now_text(),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                    VALUES ('workspace_registry_meta', 1, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        utc_now_text(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> GatewayStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
