from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import cast

from app.core.config_sources import (
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config.state import ConfigConflictError, new_config_id


def _connection_identity_fingerprint(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item.get("host"),
        item.get("port", 22),
        item.get("username"),
        item.get("private_key_path"),
        item.get("ssh_config_host"),
        item.get("remote_gateway_port", 8014),
    )
def _new_connection_id(config_path: Path, position: int) -> str:
    del config_path, position
    return new_config_id("connection")
def _jsonc_workspaces_object_starts(document: str) -> tuple[int, ...]:
    match = re.search(r'"workspaces"\s*:\s*\[', document)
    if match is None:
        raise ValueError("Gateway v1 配置缺少 workspaces 数组")
    array_start = document.find("[", match.start(), match.end())
    stack: list[str] = []
    starts: list[int] = []
    in_string = False
    escaped = False
    index = array_start
    while index < len(document):
        char = document[index]
        next_char = document[index + 1] if index + 1 < len(document) else ""
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "/" and next_char == "/":
            newline = document.find("\n", index + 2)
            index = len(document) if newline < 0 else newline
            continue
        elif char == "/" and next_char == "*":
            end = document.find("*/", index + 2)
            if end < 0:
                raise ValueError("Gateway JSONC 注释未闭合")
            index = end + 2
            continue
        elif char in "[{":
            if stack == ["["] and char == "{":
                starts.append(index)
            stack.append(char)
        elif char in "]}":
            if (
                not stack
                or (char == "]" and stack[-1] != "[")
                or (char == "}" and stack[-1] != "{")
            ):
                raise ValueError("Gateway JSONC workspaces 数组结构不平衡")
            stack.pop()
            if char == "]" and not stack:
                return tuple(starts)
        index += 1
    raise ValueError("Gateway JSONC workspaces 数组未闭合")
def _atomic_write_gateway_jsonc(path: Path, raw_bytes: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.migration-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw_bytes)
            file.flush()
            os.fsync(file.fileno())
        if path.exists():
            shutil.copymode(path, temporary_path)
        elif os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
def _migrate_gateway_connection_ids_in_source(
    *,
    raw_config: dict[str, object],
    config_path: Path,
    state_store: GatewayStateStore,
    connection_ids: tuple[str, ...],
) -> None:
    snapshot = read_stable_config_file(config_path)
    if snapshot.presence == "absent" or snapshot.raw_bytes is None:
        return
    document = snapshot.raw_bytes.decode("utf-8")
    source_config = parse_stable_config_file(snapshot)
    if source_config is None:
        raise RuntimeError(f"Gateway 配置文件解析为空: {config_path}")
    source_version = source_config.get("config_version", 1)
    source_workspaces = source_config.get("workspaces")
    record = state_store.get_config("gateway_connection_ids")
    migration = record.payload.get("migration") if record is not None else None
    if migration is not None and not isinstance(migration, dict):
        raise ValueError("Gateway connection_id migration 记录损坏")
    migration = migration if isinstance(migration, dict) else {}
    if source_version == 1 and migration.get("state") == "rolled_back":
        raw_config["config_version"] = 1
        return
    if source_version == 2 and migration.get("state") == "planned":
        backup_name = migration.get("backup_path")
        old_digest = migration.get("old_digest")
        if (
            not isinstance(backup_name, str)
            or not isinstance(old_digest, str)
            or not Path(backup_name).is_file()
            or hashlib.sha256(Path(backup_name).read_bytes()).hexdigest() != old_digest
        ):
            raise ConfigConflictError(
                "Gateway connection_id 迁移已写入新文档，但 v1 备份无法核验"
            )
        state_store.set_config(
            config_key="gateway_connection_ids",
            config_version=3,
            payload={
                "entries": record.payload.get("entries", []) if record else [],
                "migration": {
                    **migration,
                    "state": "completed",
                    "new_digest": snapshot.digest,
                },
            },
        )
        raw_config["config_version"] = 2
        return
    if source_version != 1 or not isinstance(source_workspaces, list):
        return
    missing_positions = tuple(
        index
        for index, item in enumerate(source_workspaces)
        if isinstance(item, dict) and item.get("connection_id") is None
    )
    if not missing_positions:
        return
    if len(source_workspaces) != len(connection_ids):
        raise ConfigConflictError(
            "Gateway connection_id 迁移遇到 partial source layer，拒绝写入"
        )
    migration_id = str(migration.get("migration_id") or new_config_id("migration"))
    if (
        migration.get("state") == "planned"
        and migration.get("old_digest") != snapshot.digest
    ):
        raise ConfigConflictError(
            "Gateway connection_id 迁移期间 source 文档再次变化，拒绝覆盖迁移计划"
        )
    backup_path = config_path.with_name(f"{config_path.name}.{migration_id}.v1.bak")
    old_digest = snapshot.digest
    planned_payload = {
        "entries": [
            {
                "connection_id": connection_id,
                "fingerprint": [],
            }
            for connection_id in connection_ids
        ],
        "migration": {
            "migration_id": migration_id,
            "state": "planned",
            "old_digest": old_digest,
            "new_digest": migration.get("new_digest"),
            "backup_path": str(backup_path),
            "old_version": 1,
            "new_version": 2,
        },
    }
    if (
        snapshot.digest == migration.get("new_digest")
        and migration.get("state") == "completed"
    ):
        raw_config["config_version"] = 2
        return
    state_store.set_config(
        config_key="gateway_connection_ids",
        config_version=3,
        payload=planned_payload,
    )
    verify_stable_config_file(snapshot)
    if backup_path.exists():
        if backup_path.read_bytes() != snapshot.raw_bytes:
            raise ConfigConflictError("Gateway connection_id v1 备份内容不一致")
    else:
        shutil.copy2(config_path, backup_path)
    starts = _jsonc_workspaces_object_starts(document)
    if len(starts) != len(connection_ids):
        raise ConfigConflictError(
            "Gateway connection_id 迁移无法完整定位 workspaces source layer"
        )
    migrated_document = document
    for position in reversed(missing_positions):
        start = starts[position] + 1
        migrated_document = (
            migrated_document[:start]
            + f'"connection_id": "{connection_ids[position]}", '
            + migrated_document[start:]
        )
    migrated_document, replacements = re.subn(
        r'("config_version"\s*:\s*)1\b',
        r"\g<1>2",
        migrated_document,
        count=1,
    )
    if replacements != 1:
        raise ConfigConflictError("Gateway connection_id 迁移无法定位 config_version")
    migrated_bytes = migrated_document.encode("utf-8")
    _atomic_write_gateway_jsonc(config_path, migrated_bytes)
    migrated_snapshot = read_stable_config_file(config_path)
    if migrated_snapshot.digest is None:
        raise RuntimeError("Gateway connection_id 迁移写入后无法读取文件")
    completed_payload = {
        "entries": [
            {
                "connection_id": connection_id,
                "fingerprint": [],
            }
            for connection_id in connection_ids
        ],
        "migration": {
            **planned_payload["migration"],
            "state": "completed",
            "new_digest": migrated_snapshot.digest,
        },
    }
    state_store.set_config(
        config_key="gateway_connection_ids",
        config_version=3,
        payload=completed_payload,
    )
    raw_config["config_version"] = 2
def rollback_gateway_connection_id_migration(
    *,
    config_path: Path,
    state_store: GatewayStateStore,
) -> None:
    """按 migration journal 将 Gateway 配置恢复为旧 v1 字节。"""

    record = state_store.get_config("gateway_connection_ids")
    if record is None:
        raise ConfigConflictError("Gateway connection_id 迁移记录不存在")
    migration = record.payload.get("migration")
    if not isinstance(migration, dict):
        raise ConfigConflictError("Gateway connection_id 迁移记录缺少 migration")
    migration_state = migration.get("state")
    if migration_state not in {"planned", "completed"}:
        raise ConfigConflictError(
            f"Gateway connection_id 迁移当前不可回滚: state={migration_state}"
        )
    backup_value = migration.get("backup_path")
    old_digest = migration.get("old_digest")
    new_digest = migration.get("new_digest")
    if not isinstance(backup_value, str) or not isinstance(old_digest, str):
        raise ConfigConflictError("Gateway connection_id 迁移备份元数据不完整")
    backup_path = Path(backup_value)
    if not backup_path.is_file():
        raise ConfigConflictError(f"Gateway connection_id v1 备份不存在: {backup_path}")
    backup_bytes = backup_path.read_bytes()
    if hashlib.sha256(backup_bytes).hexdigest() != old_digest:
        raise ConfigConflictError("Gateway connection_id v1 备份 digest 不匹配")
    current_snapshot = read_stable_config_file(config_path)
    if current_snapshot.presence != "present" or current_snapshot.raw_bytes is None:
        raise ConfigConflictError("Gateway connection_id 回滚目标文件不存在")
    if current_snapshot.digest not in {old_digest, new_digest}:
        raise ConfigConflictError("Gateway connection_id 回滚发现 source 已被其他修改")
    if current_snapshot.digest != old_digest:
        _atomic_write_gateway_jsonc(config_path, backup_bytes)
    state_store.set_config(
        config_key="gateway_connection_ids",
        config_version=3,
        payload={
            "entries": record.payload.get("entries", []),
            "migration": {
                **migration,
                "state": "rolled_back",
                "rollback_digest": old_digest,
                "new_digest": None,
            },
        },
    )
def _normalize_connection_ids(
    raw_config: dict[str, object],
    *,
    config_path: Path,
    state_store: GatewayStateStore | None,
) -> None:
    raw_workspaces = raw_config.get("workspaces")
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    items: list[dict[str, object]] = []
    for item in raw_workspaces:
        if not isinstance(item, dict):
            raise TypeError("Gateway workspaces 元素必须是对象")
        items.append(item)

    previous_entries: list[dict[str, object]] = []
    if state_store is not None:
        identity_record = state_store.get_config("gateway_connection_ids")
        if identity_record is not None:
            raw_entries = identity_record.payload.get("entries", [])
            if not isinstance(raw_entries, list) or not all(
                isinstance(entry, dict) for entry in raw_entries
            ):
                raise ValueError("Gateway connection_id 迁移记录损坏")
            previous_entries = cast(list[dict[str, object]], raw_entries)

    used: set[str] = set()
    unmatched_previous = list(previous_entries)
    normalized_entries: list[dict[str, object]] = []
    for position, item in enumerate(items):
        connection_id = item.get("connection_id")
        if connection_id is not None and (
            not isinstance(connection_id, str) or not connection_id
        ):
            raise ValueError("Gateway connection_id 必须是非空字符串")
        if connection_id is None:
            fingerprint = _connection_identity_fingerprint(item)
            matching = [
                entry
                for entry in unmatched_previous
                if tuple(entry.get("fingerprint", ())) == fingerprint
            ]
            if len(matching) > 1:
                raise ValueError("Gateway connection_id 迁移存在重复匹配")
            if matching:
                connection_id = cast(str, matching[0]["connection_id"])
                unmatched_previous.remove(matching[0])
            elif position < len(previous_entries):
                position_entry = previous_entries[position]
                connection_id = cast(str, position_entry["connection_id"])
                if position_entry in unmatched_previous:
                    unmatched_previous.remove(position_entry)
            else:
                connection_id = _new_connection_id(config_path, position)
        if connection_id in used:
            raise ValueError(f"Gateway connection_id 重复: {connection_id}")
        used.add(connection_id)
        item["connection_id"] = connection_id
        normalized_entries.append(
            {
                "connection_id": connection_id,
                "fingerprint": list(_connection_identity_fingerprint(item)),
            }
        )

    if state_store is not None:
        _migrate_gateway_connection_ids_in_source(
            raw_config=raw_config,
            config_path=config_path,
            state_store=state_store,
            connection_ids=tuple(cast(str, item["connection_id"]) for item in items),
        )
        identity_record = state_store.get_config("gateway_connection_ids")
        migration = (
            identity_record.payload.get("migration")
            if identity_record is not None
            else None
        )
        normalized_payload: dict[str, object] = {"entries": normalized_entries}
        if isinstance(migration, dict):
            normalized_payload["migration"] = migration
        if identity_record is None or identity_record.payload != normalized_payload:
            state_store.set_config(
                config_key="gateway_connection_ids",
                config_version=3,
                payload=normalized_payload,
            )
