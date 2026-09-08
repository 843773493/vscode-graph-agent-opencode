from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Final

import jsonschema

from app.core.config_sources import parse_stable_config_file, read_stable_config_file
from configs.runtime import merge_json_objects, validate_config

_REDACTED_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "payload_json",
        "source_baseline_json",
        "active_baseline_json",
        "secret_bindings_json",
        "health_proof_json",
        "side_effects_json",
    }
)


def _secret_binding_summary(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("secret bindings 必须是 JSON 对象")
    summary: dict[str, object] = {}
    for path, binding in parsed.items():
        if not isinstance(binding, dict):
            raise TypeError(f"secret binding 不是对象: path={path}")
        summary[str(path)] = {
            key: binding[key]
            for key in ("secret_ref", "secret_version", "binding_digest")
            if key in binding
        }
    return summary


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _secret_digest(value: object) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _inspect_jsonc_source(
    *,
    path: Path,
    layer: str,
    precedence: int,
    source_key: str | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path.expanduser().resolve()),
        "layer": layer,
        "precedence": precedence,
        "source_key": source_key,
        "presence": "absent",
        "digest": None,
        "config_version": None,
        "parse_error": None,
    }
    try:
        snapshot = read_stable_config_file(path)
        result["presence"] = snapshot.presence
        result["digest"] = snapshot.digest
        if snapshot.presence == "present":
            payload = parse_stable_config_file(snapshot)
            if payload is None:
                raise TypeError("present JSONC 快照解析为空")
            result["config_version"] = payload.get("config_version")
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        result["parse_error"] = f"{type(error).__name__}: {error}"
    return result


def _jsonc_domain_diagnostic(
    *,
    domain: str,
    layers: tuple[tuple[Path, str, int, str | None], ...],
    schema_path: Path,
) -> dict[str, object]:
    sources = [
        _inspect_jsonc_source(
            path=path,
            layer=layer,
            precedence=precedence,
            source_key=source_key,
        )
        for path, layer, precedence, source_key in layers
    ]
    result: dict[str, object] = {
        "schema_path": str(schema_path.expanduser().resolve()),
        "sources": sources,
        "effective": {
            "valid": False,
            "digest": None,
            "error": None,
        },
    }
    parsed_layers: list[dict[str, object]] = []
    for source in sources:
        if source["presence"] == "absent":
            continue
        if source["parse_error"] is not None:
            result["effective"]["error"] = (
                f"{source['path']}: {source['parse_error']}"
            )
            return result
        snapshot = read_stable_config_file(Path(str(source["path"])))
        payload = parse_stable_config_file(snapshot)
        if payload is None:
            result["effective"]["error"] = f"配置文件解析为空: {source['path']}"
            return result
        parsed_layers.append(payload)

    try:
        effective: dict[str, object] = {}
        for payload in parsed_layers:
            effective = merge_json_objects(effective, payload)
        validated = validate_config(
            effective,
            config_path=Path(f"<merged-{domain}-configuration>"),
            schema_path=schema_path,
        )
        canonical = json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result["effective"] = {
            "valid": True,
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "error": None,
        }
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        jsonschema.SchemaError,
        jsonschema.ValidationError,
    ) as error:
        result["effective"]["error"] = f"{type(error).__name__}: {error}"
    return result


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]) for row in rows}


def _read_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    *,
    table: str,
    fields: tuple[str, ...],
    order_by: str | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    if table not in tables:
        return []
    columns = _table_columns(connection, table)
    selected = tuple(field for field in fields if field in columns)
    if not selected:
        return []
    selected_sql = ", ".join(f'"{field}"' for field in selected)
    query = (
        f"SELECT {selected_sql} "
        f'FROM "{table}"'
    )
    if order_by is not None and order_by in columns:
        query += f' ORDER BY "{order_by}" DESC'
    query += " LIMIT ?"
    rows = connection.execute(query, (limit,)).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {}
        for index, field in enumerate(selected):
            if field == "secret_bindings_json":
                item["secret_bindings"] = _secret_binding_summary(row[index])
                continue
            if field in _REDACTED_JSON_FIELDS:
                continue
            value = row[index]
            if field == "fencing_token":
                item["fencing_token_digest"] = _secret_digest(value)
            else:
                item[field] = _json_value(value)
        result.append(item)
    return result


def _read_sqlite_diagnostic(*, path: Path) -> dict[str, object]:
    resolved_path = path.expanduser().resolve()
    result: dict[str, object] = {
        "path": str(resolved_path),
        "available": resolved_path.is_file(),
        "read_only": True,
        "schema_version": None,
        "tables": [],
        "sources": [],
        "source_journal": [],
        "source_fanout": [],
        "source_owner": [],
        "active": [],
        "pending": [],
        "apply_journals": [],
        "events": [],
        "conflicts": [],
    }
    if not resolved_path.is_file():
        return result

    try:
        connection = sqlite3.connect(
            resolved_path.as_uri() + "?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    try:
        tables = _table_names(connection)
        result["tables"] = sorted(tables)
        if "schema_migrations" in tables:
            rows = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            result["schema_version"] = int(rows[0]) if rows is not None else 0

        result["sources"] = _read_rows(
            connection,
            tables,
            table="config_source_layers",
            fields=(
                "config_key",
                "source_path",
                "presence",
                "config_version",
                "layer_revision",
                "layer_digest",
                "source_generation",
                "previous_digest",
                "backup_path",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["source_journal"] = _read_rows(
            connection,
            tables,
            table="config_source_journal",
            fields=(
                "source_key",
                "source_generation",
                "source_event_id",
                "source_path",
                "presence",
                "layer_revision",
                "layer_digest",
                "previous_digest",
                "origin",
                "fanout_id",
                "created_at",
            ),
            order_by="source_generation",
        )
        result["source_fanout"] = _read_rows(
            connection,
            tables,
            table="config_source_fanout",
            fields=(
                "source_key",
                "source_generation",
                "workspace_id",
                "status",
                "layer_revision",
                "layer_digest",
                "result",
                "error",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["source_owner"] = _read_rows(
            connection,
            tables,
            table="config_source_owner",
            fields=("source_key", "next_generation"),
        )
        result["active"] = _read_rows(
            connection,
            tables,
            table="config_active_snapshot",
            fields=(
                "config_domain",
                "active_revision",
                "candidate_id",
                "source_generation",
                "layer_revisions_json",
                "layer_digests_json",
                "effective_digest",
                "secret_bindings_json",
                "schema_version",
                "promoted_generation",
                "promoted_apply_id",
                "promoted_at",
                "state",
                "last_error",
            ),
            order_by="promoted_at",
        )
        result["pending"] = _read_rows(
            connection,
            tables,
            table="config_pending_candidate",
            fields=(
                "config_domain",
                "candidate_id",
                "candidate_ref",
                "idempotency_key",
                "pending_revision",
                "candidate_digest",
                "effective_digest",
                "secret_bindings_json",
                "target_generation",
                "fencing_token",
                "state",
                "last_error",
                "created_at",
                "last_attempt_id",
                "last_apply_id",
                "base_active_revision",
                "persistence_location",
                "source_generation",
            ),
            order_by="created_at",
        )
        result["apply_journals"] = _read_rows(
            connection,
            tables,
            table="config_apply_journal",
            fields=(
                "config_domain",
                "apply_id",
                "candidate_id",
                "attempt_id",
                "owner",
                "base_active_revision",
                "pending_revision",
                "registry_revision",
                "state",
                "last_error",
                "created_at",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["events"] = _read_rows(
            connection,
            tables,
            table="config_events",
            fields=(
                "event_seq",
                "event_id",
                "config_domain",
                "candidate_id",
                "attempt_id",
                "apply_id",
                "idempotency_key",
                "commit_revision",
                "active_revision",
                "pending_revision",
                "source",
                "result",
                "changed_paths_json",
                "applied_paths_json",
                "deferred_paths_json",
                "error",
                "occurred_at",
            ),
            order_by="event_seq",
        )
        result["restart_intents"] = _read_rows(
            connection,
            tables,
            table="gateway_restart_intent",
            fields=(
                "intent_id",
                "candidate_ref",
                "candidate_id",
                "base_active_revision",
                "old_generation",
                "target_generation",
                "fencing_token",
                "state",
                "requested_by",
                "last_error",
                "requested_at",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["runtime_generations"] = _read_rows(
            connection,
            tables,
            table="gateway_runtime_generation",
            fields=(
                "config_domain",
                "generation_id",
                "process_id",
                "loaded_source",
                "candidate_id",
                "active_revision",
                "pending_revision",
                "candidate_digest",
                "effective_digest",
                "secret_binding_digest",
                "fencing_token",
                "listener_state",
                "state",
                "created_at",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["registry"] = _read_rows(
            connection,
            tables,
            table="registry_meta",
            fields=("registry_key", "revision"),
        )
        result["registry_apply_journals"] = _read_rows(
            connection,
            tables,
            table="registry_apply_journal",
            fields=(
                "apply_id",
                "owner",
                "base_revision",
                "target_revision",
                "state",
                "payload_digest",
                "last_error",
                "created_at",
                "updated_at",
            ),
            order_by="updated_at",
        )
        result["conflicts"] = _diagnostic_conflicts(result)
    except sqlite3.Error as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        connection.close()
    return result


def _diagnostic_conflicts(state: dict[str, object]) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    for category in ("pending", "active", "apply_journals", "events"):
        rows = state.get(category, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            state_value = str(row.get("state", ""))
            result_value = str(row.get("result", ""))
            if state_value not in {"conflict", "recovery_required", "failed"} and (
                result_value not in {"conflict", "recovery_required", "apply_failed", "restart_failed"}
            ):
                continue
            conflicts.append(
                {
                    "category": category,
                    "candidate_id": row.get("candidate_id"),
                    "attempt_id": row.get("attempt_id"),
                    "apply_id": row.get("apply_id"),
                    "state": row.get("state"),
                    "result": row.get("result"),
                    "error": row.get("last_error", row.get("error")),
                }
            )
    return conflicts


def diagnose_configuration(
    *,
    config_root: Path,
    gateway_inline_path: Path,
    gateway_schema_path: Path,
    workspace_inline_path: Path,
    workspace_schema_path: Path,
    gateway_sqlite_path: Path,
    workspace_root: Path | None = None,
    workspace_source_owner_path: Path | None = None,
) -> dict[str, object]:
    """只读收集配置文件与状态库诊断；此函数不创建目录、不迁移、不备份。"""

    resolved_config_root = config_root.expanduser().resolve()
    gateway_layers = (
        (gateway_inline_path, "inline", 0, None),
        (resolved_config_root / "gateway.jsonc", "user", 1, "gateway_mutable_override"),
        (resolved_config_root / "gateway_local.jsonc", "user_local", 2, "gateway_local_mutable_override"),
    )
    workspace_layers = [
        (workspace_inline_path, "inline", 0, None),
        (resolved_config_root / "workspace.jsonc", "user", 1, "workspace_mutable_override"),
        (resolved_config_root / "workspace_local.jsonc", "user_local", 2, "workspace_local_mutable_override"),
    ]
    resolved_workspace = (
        workspace_root.expanduser().resolve() if workspace_root is not None else None
    )
    if resolved_workspace is not None:
        workspace_layers.append(
            (
                resolved_workspace / ".boxteam" / "workspace.jsonc",
                "workspace",
                3,
                "workspace_root_mutable_override",
            )
        )

    gateway_jsonc = _jsonc_domain_diagnostic(
        domain="gateway",
        layers=gateway_layers,
        schema_path=gateway_schema_path,
    )
    workspace_jsonc = _jsonc_domain_diagnostic(
        domain="workspace",
        layers=tuple(workspace_layers),
        schema_path=workspace_schema_path,
    )
    gateway_sqlite = _read_sqlite_diagnostic(
        path=gateway_sqlite_path,
    )
    workspace_sqlite = (
        _read_sqlite_diagnostic(
            path=resolved_workspace / ".boxteam" / "state" / "workspace.sqlite",
        )
        if resolved_workspace is not None
        else {
            "selected": False,
            "read_only": True,
            "reason": "未提供 --workspace，未选择 Workspace SQLite",
        }
    )
    workspace_source_owner = _read_sqlite_diagnostic(
        path=(
            workspace_source_owner_path
            or resolved_config_root / "workspace-source.sqlite"
        ),
    )
    return {
        "action": "diagnose",
        "read_only": True,
        "config_root": str(resolved_config_root),
        "migration": {
            "executed": False,
            "backup_created": False,
            "user_layout_migration": "not_run",
            "workspace_layout_migration": (
                "not_selected" if resolved_workspace is None else "not_run"
            ),
        },
        "gateway": {
            "jsonc": gateway_jsonc,
            "sqlite": gateway_sqlite,
        },
        "workspace": {
            "root": str(resolved_workspace) if resolved_workspace is not None else None,
            "jsonc": workspace_jsonc,
            "sqlite": workspace_sqlite,
            "source_owner": workspace_source_owner,
        },
    }
