"""注册表快照的唯一只读投影；连接和事务由调用方持有。"""

from __future__ import annotations

import json
import sqlite3


def read_workspace_registry(connection: sqlite3.Connection) -> dict[str, object] | None:
    """在调用方的同一快照内读取；离线预检不初始化 store 或触发迁移。"""
    meta = connection.execute(
        "SELECT payload_json FROM gateway_config WHERE config_key = 'workspace_registry_meta'"
    ).fetchone()
    metadata = json.loads(str(meta[0])) if meta is not None else {}
    if not isinstance(metadata, dict):
        raise ValueError("Gateway SQLite 配置不是对象: key=workspace_registry_meta")  # noqa: TRY004 -- 保留既有拒绝类型。
    rows = connection.execute(
        "SELECT payload_json FROM gateway_workspace_registry ORDER BY position ASC, workspace_id ASC"
    ).fetchall()
    if meta is None and not rows:
        return None
    targets: list[object] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("Gateway SQLite 工作区注册记录必须是对象")  # noqa: TRY004 -- 保留既有拒绝类型。
        targets.append(payload)
    return {
        "schema_version": int(metadata.get("schema_version", 10)),
        "registry_revision": int(metadata.get("registry_revision", 0)),
        "active_workspace_id": metadata.get("active_workspace_id"),
        "order_customized": bool(metadata.get("order_customized", False)),
        "runtime_generation": metadata.get("runtime_generation"),
        "remote_gateway_connections": metadata.get("remote_gateway_connections", []),
        "managed_backends": metadata.get("managed_backends", []),
        "targets": targets,
    }


