"""日志跨域迁移的 SQLite 正文转换；不创建运行时 store 或改写历史事件。"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import cast

from app.core.config_sources import config_revision
from app.core.sqlite_state import utc_now_text
from app.gateway.config.connection_ids import (
    _CONNECTION_ID_RECORD_VERSION,
    _normalize_connection_ids,
)
from app.services.infrastructure.config.state import (
    restore_environment_secret_references,
)
from configs.runtime import merge_json_objects
from configs.runtime_logging_migration import (
    TARGET_CONFIG_VERSIONS,
    ConfigDomain,
    RuntimeLoggingMigrationPlan,
    read_connection_identity,
)

MUTABLE_TABLES = ("config_source_layers", "config_active_snapshot", "config_revision_meta")


def migrated_payload(
    payload: dict[str, object], domain: ConfigDomain, plan: RuntimeLoggingMigrationPlan,
    connection_entries: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    result = deepcopy(payload)
    if domain == "workspace":
        result.pop("logger", None)
    else:
        features = result.get("features")
        if isinstance(features, dict):
            features.pop("session_generators", None)
        if "workspaces" in result:
            entries = _normalize_connection_ids(
                result, config_path=Path.cwd(), state_store=None, previous_entries=connection_entries,
            )
            if connection_entries is not None:
                connection_entries[:] = entries
        logging = result.setdefault("logging", {}) if plan.settings is not None else result.get("logging")
        if isinstance(logging, dict):
            if plan.settings is not None:
                logging["runtime"] = dict(plan.settings)
            runtime = logging.get("runtime")
            if isinstance(runtime, dict) and isinstance(runtime.get("level"), str):
                runtime["level"] = runtime["level"].strip().lower()
    result["config_version"] = TARGET_CONFIG_VERSIONS[domain]
    return result


def configuration_rows(connection: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    """CAS 覆盖整个配置账本，不能在迁移期间跨过新增的候选或 claim。"""
    connection.row_factory = sqlite3.Row
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND (name GLOB 'config_*' OR name='gateway_config') ORDER BY name"
    ).fetchall()
    return {
        str(table[0]): sorted(
            [dict(row) for row in connection.execute(f'SELECT * FROM "{table[0]}"')],
            key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False),
        )
        for table in tables
    }


def migrate_database(
    connection: sqlite3.Connection, *, domain: ConfigDomain,
    source_path: Path, plan: RuntimeLoggingMigrationPlan,
    file_digests: dict[str, tuple[str | None, str | None]],
    inline_config: dict[str, object],
    connection_entries: list[dict[str, object]] | None = None,
) -> None:
    connection.row_factory = sqlite3.Row
    if domain == "gateway":
        identity = read_connection_identity(connection)
        if identity is not None and identity[0] != _CONNECTION_ID_RECORD_VERSION:
            # v3 的 entries 与稳定 ID 原样保留，只升级格式标记；不触碰其它 Gateway 记录。
            connection.execute(
                "UPDATE gateway_config SET config_version=? WHERE config_key='gateway_connection_ids'",
                (_CONNECTION_ID_RECORD_VERSION,),
            )
    selected = {item.key for item in plan.inputs if item.path == source_path and item.kind == "source_layer"}
    references: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for row in connection.execute("SELECT * FROM config_source_layers ORDER BY config_key").fetchall():
        old = dict(row)
        key = str(old["config_key"])
        if key not in selected:
            continue
        payload = json.loads(str(old["payload_json"])) if old["payload_json"] is not None else None
        changed = migrated_payload(payload, domain, plan, connection_entries) if payload is not None else None
        version = TARGET_CONFIG_VERSIONS[domain]
        if changed == payload and old["config_version"] == version:
            continue
        digest = config_revision(changed) if changed is not None else None
        filename = {"gateway_mutable_override": "gateway.jsonc", "gateway_local_mutable_override": "gateway_local.jsonc",
                    "workspace_mutable_override": "workspace.jsonc", "workspace_local_mutable_override": "workspace_local.jsonc",
                    "workspace_root_mutable_override": "workspace.jsonc"}[key]
        for file, (before, after) in file_digests.items():
            path = Path(file)
            is_workspace_root = path.parent.name == ".boxteam"
            if path.name == filename and is_workspace_root == (key == "workspace_root_mutable_override") and before == old["layer_digest"] and before is not None:
                digest = after
                break
        revision = int(old["layer_revision"]) + 1
        # 共享用户来源的 generation 属于 WorkspaceSourceOwner；下次正常观察由它推进。
        generation = int(old["source_generation"]) + (key != "workspace_mutable_override" or domain != "workspace")
        connection.execute(
            "UPDATE config_source_layers SET config_version=?, payload_json=?, layer_revision=?, "
            "layer_digest=?, source_generation=?, previous_digest=?, previous_payload_json=?, updated_at=? WHERE config_key=?",
            (version, _dump(changed) if changed is not None else None, revision, digest, generation, old["layer_digest"], old["payload_json"], utc_now_text(), key),
        )
        references[key] = (old, {"layer_revision": revision, "layer_digest": digest, "source_generation": generation})
    row = connection.execute(
        "SELECT * FROM config_active_snapshot WHERE config_domain=?", (domain,),
    ).fetchone()
    if row is None:
        return
    old = dict(row)
    payload = json.loads(str(old["payload_json"]))
    changed = migrated_payload(payload, domain, plan, connection_entries)
    if domain == "workspace":
        # 显式离线升级补齐旧 active 缺失的新字段，保留已有取值；来源层仍是局部覆盖。
        changed = merge_json_objects(inline_config, changed)
    if domain == "gateway":
        logging = changed.setdefault("logging", {})
        if not isinstance(logging, dict):
            raise TypeError("Gateway logging 必须是对象")
        runtime = logging.setdefault("runtime", {})
        if not isinstance(runtime, dict):
            raise TypeError("Gateway logging.runtime 必须是对象")
        runtime.setdefault("level", plan.contract.default_level)
        runtime.setdefault("pretty", plan.contract.default_pretty)
    # Workspace 启动按还原后的用户配置校验摘要，不能对 env:NAME 存储格式记账。
    effective = restore_environment_secret_references(changed) if domain == "workspace" else changed
    effective_digest = config_revision(cast(dict[str, object], effective))
    if (payload == changed and not references and old["schema_version"] == TARGET_CONFIG_VERSIONS[domain]
            and old["effective_digest"] == effective_digest):
        return
    baseline = json.loads(str(old["source_baseline_json"]))
    revisions = json.loads(str(old["layer_revisions_json"]))
    digests = json.loads(str(old["layer_digests_json"]))
    for key, (prior, current) in references.items():
        bound = baseline.get(key)
        if isinstance(bound, dict) and (
            bound.get("layer_revision") == prior["layer_revision"]
            and bound.get("layer_digest") == prior["layer_digest"]
        ):
            bound.update(current)
            revisions[key] = current["layer_revision"]
            digests[key] = current["layer_digest"]
    allocator = connection.execute(
        "SELECT next_revision FROM config_revision_meta WHERE config_domain=?", (domain,),
    ).fetchone()
    revision = max(int(old["active_revision"]) + 1, int(allocator[0]) if allocator else 1)
    connection.execute(
        "INSERT INTO config_revision_meta(config_domain,next_revision) VALUES (?,?) "
        "ON CONFLICT(config_domain) DO UPDATE SET next_revision=excluded.next_revision", (domain, revision + 1),
    )
    generations = [int(value["source_generation"]) for value in baseline.values()
                   if isinstance(value, dict) and value.get("source_generation") is not None]
    connection.execute(
        "UPDATE config_active_snapshot SET active_revision=?, payload_json=?, source_baseline_json=?, "
        "source_generation=?, layer_revisions_json=?, layer_digests_json=?, effective_digest=?, schema_version=? "
        "WHERE config_domain=?",
        (revision, _dump(changed), _dump(baseline), max([int(old["source_generation"]), *generations]),
         _dump(revisions), _dump(digests), effective_digest, TARGET_CONFIG_VERSIONS[domain], domain),
    )


def replace_configuration_rows(connection: sqlite3.Connection, source: sqlite3.Connection) -> None:
    """替换可变配置表及单条连接身份；其它业务数据和不可变历史不受恢复影响。"""
    source.row_factory = sqlite3.Row
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='gateway_config'").fetchone():
        identity = source.execute(
            "SELECT config_version,payload_json,updated_at FROM gateway_config WHERE config_key='gateway_connection_ids'"
        ).fetchone()
        if identity is not None:
            connection.execute(
                "INSERT INTO gateway_config(config_key,config_version,payload_json,updated_at) "
                "VALUES ('gateway_connection_ids',?,?,?) ON CONFLICT(config_key) DO UPDATE SET "
                "config_version=excluded.config_version,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                tuple(identity),
            )
    for table in MUTABLE_TABLES:
        columns = list(source.execute(f'PRAGMA table_info("{table}")'))
        names = [str(column[1]) for column in columns]
        primary = [str(column[1]) for column in columns if column[5]]
        rows = [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')]
        current = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
        for row in current:
            if not any(all(row[key] == target[key] for key in primary) for target in rows):
                connection.execute(
                    f'DELETE FROM "{table}" WHERE ' + " AND ".join(f'"{key}"=?' for key in primary),
                    tuple(row[key] for key in primary),
                )
        assignments = ",".join(f'"{name}"=excluded."{name}"' for name in names if name not in primary)
        sql = (f'INSERT INTO "{table}" (' + ",".join(f'"{name}"' for name in names)
               + ") VALUES (" + ",".join("?" for _ in names) + ") ON CONFLICT("
               + ",".join(f'"{key}"' for key in primary) + f") DO UPDATE SET {assignments}")
        connection.executemany(sql, [tuple(row[name] for name in names) for row in rows])


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
