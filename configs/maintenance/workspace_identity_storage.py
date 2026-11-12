"""显式身份迁移的控制面转换；旧别名只在维护事务中解析。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.gateway.control.gateway_registry.snapshot import read_workspace_registry
from app.gateway.control.gateway_state._schema import _migrate_workspace_owner_schema
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from configs.maintenance.workspace_identity_migration import WorkspaceIdentityPreview


def database_digest(connection: sqlite3.Connection) -> str:
    """维护窗口比较完整逻辑状态，防止回退覆盖迁移后的控制面写入。"""
    return hashlib.sha256("\n".join(sorted(connection.iterdump())).encode()).hexdigest()


def migrated_registry(payload: dict[str, object], preview: WorkspaceIdentityPreview) -> dict[str, object]:
    mappings = {item.legacy_workspace_id: item for item in preview.mappings}
    old_targets = payload["targets"]
    assert isinstance(old_targets, list)

    def owner(alias: object) -> dict[str, str] | None:
        if alias is None:
            return None
        if not isinstance(alias, str) or alias not in mappings:
            raise ValueError(f"迁移引用未登记的旧工作区: {alias}")
        item = mappings[alias]
        return {"gateway_id": item.gateway_id, "workspace_id": item.workspace_id}

    targets: dict[tuple[str, str], dict[str, object]] = {}
    connections = {item["connection_id"]: dict(item) for item in cast(list[dict[str, object]], payload.get("remote_gateway_connections", []))}
    valid_fields = {item.name for item in fields(WorkspaceTarget)}
    for raw in old_targets:
        record = dict(raw)
        alias = record["workspace_id"]
        mapping = mappings[alias]
        record.update(owner(alias) or {})
        record["parent_workspace"] = owner(record.pop("parent_workspace_id", None))
        connection_id = record.pop("remote_gateway_connection_id", None)
        cursor = record.pop("remote_config_event_cursor", None)
        record["remote_gateway_connection_ids"] = [] if connection_id is None else [connection_id]
        if connection_id is not None:
            connection = connections[connection_id]
            if cursor is not None:
                stored_cursor = connection.get("remote_config_event_cursor")
                if stored_cursor is not None and stored_cursor != cursor:
                    raise ValueError(f"旧连接与投影游标不一致，保留原件: {connection_id}")
                connection["remote_config_event_cursor"] = cursor
            connection["protocol_version"] = FEDERATION_PROTOCOL_VERSION
            record["connection_id"] = None
            record["target_namespace"] = "gateway"
            affected = []
            for value in record.get("remote_affected_workspace_ids", []):
                member = mappings[value]
                if member.gateway_id != mapping.gateway_id or member.connection_id != connection_id:
                    raise ValueError(f"旧远端完整组包含另一来源的工作区: {value}")
                affected.append(member.workspace_id)
            record["remote_affected_workspace_ids"] = affected
        record.pop("backend_workspace_id", None)
        record.pop("remote_workspace_id", None)
        if set(record) - valid_fields:
            raise ValueError(f"旧工作区包含无法迁移的字段: {sorted(set(record) - valid_fields)}")
        key = mapping.gateway_id, mapping.workspace_id
        previous = targets.get(key)
        if previous is not None:
            connection_fields = {"backend_url", "remote_gateway_connection_ids", "target_generation", "runtime_lease_id", "connection_error", "name"}
            if {k: v for k, v in previous.items() if k not in connection_fields} != {
                k: v for k, v in record.items() if k not in connection_fields
            } or (record.get("name_customized") and previous["name"] != record["name"]):
                raise ValueError(f"同一来源工作区的多连接记录冲突，保留原件: {key}")
            assert isinstance(connection_id, str)
            previous["remote_gateway_connection_ids"] = sorted(set(cast(list[str], previous["remote_gateway_connection_ids"])) | {connection_id})
        else:
            targets[key] = record
    backends = []
    for raw in cast(list[dict[str, object]], payload.get("managed_backends", [])):
        record = dict(raw)
        record["gateway_id"] = preview.gateway_id
        record["members"] = [{"workspace_id": mappings[cast(str, item["workspace_id"])].workspace_id}
                             for item in cast(list[dict[str, object]], raw["members"])]
        backends.append(record)
    result = dict(payload)
    result.pop("active_workspace_id", None)
    result.update(schema_version=14, gateway_id=preview.gateway_id,
                  active_workspace=owner(payload.get("active_workspace_id")),
                  managed_backends=backends, targets=list(targets.values()),
                  remote_gateway_connections=list(connections.values()))
    if "registry_revision" in result:
        result["registry_revision"] = cast(int, result["registry_revision"]) + 1
    return result


def migrated_navigation(payload: dict[str, object], preview: WorkspaceIdentityPreview) -> dict[str, object]:
    mappings = {item.legacy_workspace_id: item for item in preview.mappings}
    nodes = []
    for raw in cast(list[dict[str, object]], payload["nodes"]):
        record = dict(raw)
        alias = record.pop("workspace_id", None)
        if record["kind"] == "workspace_ref":
            item = mappings[cast(str, alias)]
            record["workspace"] = {"gateway_id": item.gateway_id, "workspace_id": item.workspace_id}
        nodes.append(record)
    return {**payload, "schema_version": 2, "nodes": nodes}


def migrate_database(connection: sqlite3.Connection, preview: WorkspaceIdentityPreview) -> None:
    """同一事务迁齐 payload、视图归属及 schema；保留所有非定位字段与历史 journal。"""
    payload = read_workspace_registry(connection)
    if payload is None:
        raise ValueError("身份迁移没有权威 registry 输入")
    result = migrated_registry(payload, preview)
    owners = {(item["gateway_id"], item["workspace_id"]): item
              for item in cast(list[dict[str, object]], result["targets"])}
    mappings = {item.legacy_workspace_id: item for item in preview.mappings}
    # 同一资源的多个旧别名先归一正文，再由原 schema 19 合并资源键。
    for alias, mapping in mappings.items():
        connection.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id=?",
                           (json.dumps(owners[(mapping.gateway_id, mapping.workspace_id)], ensure_ascii=False), alias))
    # 视图碰撞不能任意挑一个；相同完整状态只保留一个原主键供结构升级关联。
    seen_views: dict[tuple[str, str, str, str], tuple[str, tuple[object, ...]]] = {}
    for row in connection.execute("SELECT * FROM user_view_state ORDER BY user_id,workspace_id,session_id").fetchall():
        item = mappings[row["workspace_id"]]
        key = row["user_id"], item.gateway_id, item.workspace_id, row["session_id"]
        value = tuple(row[name] for name in row.keys() if name != "workspace_id")  # noqa: SIM118 -- sqlite3.Row 迭代值，keys() 才返回列名。
        if key in seen_views:
            if value != seen_views[key][1]:
                raise ValueError(f"用户视图在身份合并后冲突，保留全部原记录: {key}")
            connection.execute("DELETE FROM user_view_state WHERE user_id=? AND workspace_id=? AND session_id=?",
                               (row["user_id"], row["workspace_id"], row["session_id"]))
        else:
            seen_views[key] = row["workspace_id"], value
    metadata = {key: value for key, value in result.items() if key != "targets"}
    connection.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'",
                       (json.dumps(metadata, ensure_ascii=False),))
    connection.execute("UPDATE registry_meta SET revision=? WHERE registry_key='workspace'", (result["registry_revision"],))
    _migrate_workspace_owner_schema(connection)
    connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (19,?)", (utc_now_text(),))
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("身份迁移后的控制面外键不完整")


def replace_database(connection: sqlite3.Connection, source: sqlite3.Connection) -> None:
    """复用维护发布事务；只恢复本次变更的表，DDL 取自已核验同批备份。"""
    for table in ("gateway_workspace_registry", "user_view_state", "gateway_config", "registry_meta", "schema_migrations"):
        definitions = source.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL ORDER BY type='table' DESC,name", (table,),
        ).fetchall()
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(definitions[0][0])
        columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', source.execute(f'SELECT * FROM "{table}"'))
        for definition in definitions[1:]:
            connection.execute(definition[0])
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("发布身份迁移后的控制面外键不完整")


def validate_registry_file(path: Path, gateway_id: str) -> None:
    """候选文件走生产唯一 registry parser，避免维护工具另写一套新合同。"""
    registry = GatewayWorkspaceRegistry(storage_path=path, gateway_id=gateway_id)
    registry.close()
