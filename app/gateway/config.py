from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from app.core.config_sources import (
    ConfigSource,
    config_revision,
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.core.history_loading import (
    DEFAULT_ANCHOR_AFTER_TURNS,
    DEFAULT_ANCHOR_BEFORE_TURNS,
    DEFAULT_ANCHOR_INCLUDE,
    DEFAULT_INITIAL_INCLUDE,
    DEFAULT_INITIAL_TURNS,
    HistoryLoadingConfig,
)
from app.core.path_utils import (
    get_user_config_root,
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config import ConfigFileWatcher, ConfigReloadStatus
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    SecretReferenceRequiredError,
    build_secret_binding_summary,
    changed_json_paths,
    dump_json,
    new_config_id,
    prepare_config_for_persistence,
    redact_config_payload,
)
from configs.installer import resolve_config_resource_source
from configs.runtime import merge_json_objects, read_jsonc_object, validate_config


@dataclass(frozen=True, slots=True)
class ConfiguredRemoteGateway:
    host: str
    username: str
    private_key_path: str
    kind: Literal["remote_gateway"] = "remote_gateway"
    connection_id: str = ""
    name: str | None = None
    port: int = 22
    ssh_config_host: str | None = None
    remote_pair_command: str | None = None
    remote_gateway_port: int = 8014
    activate: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ConfiguredTheme:
    id: str
    label: str
    extends: Literal["warm", "green", "blue"]
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background: dict[str, object] | None = None


GatewayHistoryLoadingConfig = HistoryLoadingConfig

REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS = frozenset(
    {
        "catalog-generator-scheduler",
        "health-controller",
        "registry-batch",
        "ssh-tunnel-proxy",
        "workspace-process",
        "remote-projection",
    }
)


def record_gateway_restart_startup_failure(
    *,
    state_store: GatewayStateStore,
    candidate_ref: str,
    error: str,
    gateway_id: str,
    target_generation: str,
    fencing_token: str,
) -> None:
    """在 Gateway 配置解析尚未完成时，把 owned pending 固定到恢复态。"""

    if not error:
        raise ValueError("Gateway restart_failed 错误不能为空")
    state_store.record_gateway_restart_startup_failure(
        candidate_ref=candidate_ref,
        gateway_id=gateway_id,
        target_generation=target_generation,
        fencing_token=fencing_token,
        error=error,
    )


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    workspaces: tuple[ConfiguredRemoteGateway, ...] = ()
    default_theme_id: str = "warm"
    custom_themes: tuple[ConfiguredTheme, ...] = ()
    session_catalog_refresh_interval_seconds: float = 30
    session_catalog_max_concurrency: int = 8
    session_catalog_request_timeout_seconds: float = 30
    session_generator_poll_interval_seconds: float = 1
    gateway_process_health_request_timeout_seconds: float = 2
    gateway_process_health_poll_interval_seconds: float = 0.5
    gateway_process_connection_drain_timeout_seconds: float = 2
    default_workspace_skill_groups: tuple[str, ...] = ()
    history_loading: GatewayHistoryLoadingConfig = field(
        default_factory=GatewayHistoryLoadingConfig,
    )
    revision: str = ""
    schema_path: Path | None = None
    source_paths: tuple[Path, ...] = ()
    source_details: tuple[ConfigSource, ...] = ()
    payload: dict[str, object] = field(default_factory=dict)


def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredRemoteGateway:
    return ConfiguredRemoteGateway(
        name=cast(str | None, raw.get("name")),
        host=cast(str, raw["host"]),
        port=cast(int, raw.get("port", 22)),
        ssh_config_host=cast(str | None, raw.get("ssh_config_host")),
        remote_pair_command=cast(str | None, raw.get("remote_pair_command")),
        username=cast(str, raw["username"]),
        private_key_path=cast(str, raw["private_key_path"]),
        connection_id=cast(str, raw["connection_id"]),
        remote_gateway_port=cast(int, raw.get("remote_gateway_port", 8014)),
        activate=cast(bool, raw.get("activate", False)),
        enabled=cast(bool, raw.get("enabled", True)),
    )


def _nested_config_value(raw: dict[str, object], *keys: str) -> object | None:
    current: object = raw
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _positive_number_config(
    raw: dict[str, object],
    *keys: str,
    default: float,
) -> float:
    value = _nested_config_value(raw, *keys)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def _positive_integer_config(
    raw: dict[str, object],
    *keys: str,
    default: int,
) -> int:
    value = _nested_config_value(raw, *keys)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _skill_groups_config(raw: dict[str, object]) -> tuple[str, ...]:
    value = _nested_config_value(raw, "runtime", "workspace", "default_skill_groups")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("runtime.workspace.default_skill_groups 必须是字符串数组")
    return tuple(value)


def _history_loading_config(raw: dict[str, object]) -> GatewayHistoryLoadingConfig:
    initial_value = _nested_config_value(
        raw,
        "features",
        "session_history",
        "loading",
        "progressive",
        "initial",
    )
    anchor_value = _nested_config_value(
        raw,
        "features",
        "session_history",
        "loading",
        "progressive",
        "anchor",
    )
    initial = cast(dict[str, object], initial_value or {})
    anchor = cast(dict[str, object], anchor_value or {})
    initial_turns = initial.get("turns", DEFAULT_INITIAL_TURNS)
    anchor_before_turns = anchor.get("before_turns", DEFAULT_ANCHOR_BEFORE_TURNS)
    anchor_after_turns = anchor.get("after_turns", DEFAULT_ANCHOR_AFTER_TURNS)
    initial_include = initial.get(
        "include",
        list(DEFAULT_INITIAL_INCLUDE),
    )
    anchor_include = anchor.get("include", list(DEFAULT_ANCHOR_INCLUDE))
    if (
        isinstance(initial_turns, bool)
        or not isinstance(initial_turns, int)
        or initial_turns < 1
        or isinstance(anchor_before_turns, bool)
        or not isinstance(anchor_before_turns, int)
        or anchor_before_turns < 1
        or isinstance(anchor_after_turns, bool)
        or not isinstance(anchor_after_turns, int)
        or anchor_after_turns < 1
        or not isinstance(initial_include, list)
        or not all(isinstance(item, str) for item in initial_include)
        or not isinstance(anchor_include, list)
        or not all(isinstance(item, str) for item in anchor_include)
    ):
        raise TypeError("Gateway 历史加载配置结构非法")
    return GatewayHistoryLoadingConfig(
        initial_turns=initial_turns,
        initial_include=tuple(initial_include),
        anchor_before_turns=anchor_before_turns,
        anchor_after_turns=anchor_after_turns,
        anchor_include=tuple(anchor_include),
    )


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


def _gateway_source_detail(
    state_store: GatewayStateStore,
    config_key: str,
    *,
    precedence: int,
    fallback_path: Path,
) -> ConfigSource:
    record = state_store.get_source_layer(config_key)
    legacy = state_store.get_config(config_key)
    return ConfigSource(
        path=state_store.path,
        layer="sqlite",
        precedence=precedence,
        loaded=(
            record.presence == "present"
            if record is not None
            else legacy is not None or fallback_path.is_file()
        ),
        source_key=config_key,
        presence=(
            record.presence
            if record is not None
            else "present"
            if legacy is not None or fallback_path.is_file()
            else "absent"
        ),
        layer_revision=record.layer_revision if record is not None else None,
        layer_digest=record.layer_digest if record is not None else None,
        source_generation=record.source_generation if record is not None else None,
    )


def load_gateway_config(
    *,
    config_path: Path | None = None,
    schema_path: Path | None = None,
    local_config_path: Path | None = None,
    inline_config_path: Path | None = None,
    state_store: GatewayStateStore | None = None,
    persist_migrations: bool = True,
    startup: bool = False,
    gateway_id: str | None = None,
) -> GatewayConfig:
    """加载 Gateway 内置默认、用户配置和本地覆盖。"""
    resolved_config_path = config_path or get_user_gateway_config_path()
    resolved_schema_path = schema_path or get_user_gateway_schema_path()
    resolved_inline_config_path = inline_config_path or resolve_config_resource_source(
        "gateway_inline.jsonc"
    )
    resolved_local_config_path = local_config_path or (
        get_user_gateway_local_config_path()
        if config_path is None
        else resolved_config_path.parent / "gateway_local.jsonc"
    )
    raw_gateway_config = read_jsonc_object(resolved_inline_config_path)
    source_paths: list[Path] = [resolved_inline_config_path]
    user_override: dict[str, object] | None = None
    local_override: dict[str, object] | None = None
    source_details: list[ConfigSource] = [
        ConfigSource(
            path=resolved_inline_config_path,
            layer="inline",
            precedence=0,
            loaded=True,
        )
    ]
    loaded_persisted_snapshot = False
    if startup and state_store is not None:
        blocked = state_store.migrate_legacy_active_snapshot_secrets(
            config_domain="gateway"
        )
        if blocked:
            raise SecretReferenceRequiredError(
                "旧 Gateway active snapshot 的 secret 必须重新导入引用: "
                + ", ".join(blocked)
            )
        candidate_ref = os.environ.get("BOXTEAM_CONFIG_CANDIDATE_REF")
        persisted_payload: dict[str, object] | None = None
        persisted_source_key: str | None = None
        persisted_digest: str | None = None
        persisted_generation: int | None = None
        if candidate_ref:
            pending = state_store.load_gateway_pending_candidate(
                candidate_ref=candidate_ref,
                gateway_id=gateway_id,
            )
            generation = os.environ.get("BOXTEAM_CONFIG_GENERATION")
            fencing_token = os.environ.get("BOXTEAM_CONFIG_FENCING_TOKEN")
            if not generation or not fencing_token:
                raise ConfigConflictError(
                    "Gateway pending 启动缺少 generation 或 fencing token"
                )
            if pending.target_generation != generation:
                raise ConfigConflictError(
                    "Gateway pending 启动 generation 不匹配: "
                    f"candidate={pending.target_generation}, process={generation}"
                )
            if pending.fencing_token != fencing_token:
                raise ConfigConflictError("Gateway pending 启动 fencing token 不匹配")
            persisted_payload = pending.payload
            persisted_source_key = "pending_snapshot"
            persisted_digest = pending.effective_digest
            persisted_generation = None
        else:
            active = state_store.get_active_config_snapshot("gateway")
            if active is not None:
                if active.state != "active":
                    raise ConfigConflictError(
                        "Gateway active snapshot 当前需要恢复，禁止继续启动: "
                        f"state={active.state}, error={active.last_error}"
                    )
                persisted_payload = active.payload
                persisted_source_key = "active_snapshot"
                persisted_digest = active.effective_digest
                persisted_generation = active.source_generation
        if persisted_payload is not None:
            raw_gateway_config = dict(persisted_payload)
            source_paths = [resolved_inline_config_path, state_store.path]
            source_details = [
                source_details[0],
                ConfigSource(
                    path=state_store.path,
                    layer="sqlite",
                    precedence=1,
                    loaded=True,
                    source_key=persisted_source_key,
                    layer_digest=persisted_digest,
                    source_generation=(
                        int(persisted_generation)
                        if persisted_generation is not None
                        else None
                    ),
                ),
            ]
            loaded_persisted_snapshot = True
    if state_store is None:
        source_details.extend(
            [
                ConfigSource(
                    path=resolved_config_path,
                    layer="user",
                    precedence=1,
                    loaded=resolved_config_path.is_file(),
                    source_key="gateway_mutable_override",
                    presence=(
                        "present" if resolved_config_path.is_file() else "absent"
                    ),
                ),
                ConfigSource(
                    path=resolved_local_config_path,
                    layer="user_local",
                    precedence=2,
                    loaded=resolved_local_config_path.is_file(),
                    source_key="gateway_local_mutable_override",
                    presence=(
                        "present" if resolved_local_config_path.is_file() else "absent"
                    ),
                ),
            ]
        )
        if resolved_config_path.is_file():
            raw_gateway_config = merge_json_objects(
                raw_gateway_config,
                read_jsonc_object(resolved_config_path),
            )
            source_paths.append(resolved_config_path)
        if resolved_local_config_path.is_file():
            raw_gateway_config = merge_json_objects(
                raw_gateway_config,
                read_jsonc_object(resolved_local_config_path),
            )
            source_paths.append(resolved_local_config_path)
    elif not loaded_persisted_snapshot:
        user_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_mutable_override",
            path=resolved_config_path,
            persist_migrations=persist_migrations,
        )
        local_override = _load_or_migrate_gateway_override(
            state_store=state_store,
            config_key="gateway_local_mutable_override",
            path=resolved_local_config_path,
            persist_migrations=persist_migrations,
        )
        source_details.extend(
            [
                _gateway_source_detail(
                    state_store,
                    "gateway_mutable_override",
                    precedence=1,
                    fallback_path=resolved_config_path,
                ),
                _gateway_source_detail(
                    state_store,
                    "gateway_local_mutable_override",
                    precedence=2,
                    fallback_path=resolved_local_config_path,
                ),
            ]
        )
    if user_override is not None:
        raw_gateway_config = merge_json_objects(raw_gateway_config, user_override)
        if state_store.path not in source_paths:
            source_paths.append(state_store.path)
    if local_override is not None:
        raw_gateway_config = merge_json_objects(raw_gateway_config, local_override)
        if state_store.path not in source_paths:
            source_paths.append(state_store.path)
    validate_config(
        raw_gateway_config,
        config_path=resolved_config_path,
        schema_path=resolved_schema_path,
    )
    _normalize_connection_ids(
        raw_gateway_config,
        config_path=resolved_config_path,
        state_store=(
            state_store
            if persist_migrations and not loaded_persisted_snapshot
            else None
        ),
    )
    # 旧版先按 v1 结构校验，再校验补入 connection_id 的规范化候选。
    validate_config(
        raw_gateway_config,
        config_path=resolved_config_path,
        schema_path=resolved_schema_path,
    )
    raw_workspaces = raw_gateway_config["workspaces"]
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    validated_workspaces = cast(list[dict[str, object]], raw_workspaces)
    raw_ui = cast(dict[str, object], raw_gateway_config.get("ui", {}))
    raw_theme = cast(dict[str, object], raw_ui.get("theme", {}))
    raw_custom_themes = cast(
        list[dict[str, object]], raw_theme.get("custom_themes", [])
    )
    return GatewayConfig(
        workspaces=tuple(
            workspace
            for item in validated_workspaces
            if cast(bool, item.get("enabled", True))
            if (workspace := _workspace_from_validated_config(item)).enabled
        ),
        default_theme_id=cast(str, raw_theme.get("default_theme_id", "warm")),
        custom_themes=tuple(
            ConfiguredTheme(
                id=cast(str, item["id"]),
                label=cast(str, item["label"]),
                extends=cast(Literal["warm", "green", "blue"], item["extends"]),
                color_scheme=cast(
                    Literal["light", "dark"], item.get("color_scheme", "light")
                ),
                tokens=cast(dict[str, str], item.get("tokens", {})),
                background=_configured_theme_background(
                    cast(dict[str, object] | None, item.get("background")),
                    config_root=resolved_config_path.parent,
                ),
            )
            for item in raw_custom_themes
        ),
        # 这些运行时默认值来自 inline 配置；schema 校验已在上方完成，读取失败时保留
        # 旧常量作为仅针对旧自定义 inline 文件的兼容兜底。
        session_catalog_refresh_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "refresh_interval_seconds",
            default=30,
        ),
        session_catalog_max_concurrency=_positive_integer_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "max_concurrency",
            default=8,
        ),
        session_catalog_request_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_catalog",
            "sync",
            "request_timeout_seconds",
            default=30,
        ),
        session_generator_poll_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "features",
            "session_generators",
            "scheduler",
            "poll_interval_seconds",
            default=1,
        ),
        gateway_process_health_request_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "health",
            "request_timeout_seconds",
            default=2,
        ),
        gateway_process_health_poll_interval_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "health",
            "poll_interval_seconds",
            default=0.5,
        ),
        gateway_process_connection_drain_timeout_seconds=_positive_number_config(
            raw_gateway_config,
            "runtime",
            "gateway",
            "process",
            "lifecycle",
            "connection_drain_timeout_seconds",
            default=2,
        ),
        default_workspace_skill_groups=_skill_groups_config(raw_gateway_config),
        history_loading=_history_loading_config(raw_gateway_config),
        revision=config_revision(raw_gateway_config),
        schema_path=resolved_schema_path,
        source_paths=tuple(source_paths),
        source_details=tuple(source_details),
        payload=dict(raw_gateway_config),
    )


def _normalize_consumer_health_digests(
    digests: dict[str, str],
) -> dict[str, str]:
    if not isinstance(digests, dict):
        raise ConfigConflictError("Gateway consumer health digest 必须是对象")
    normalized: dict[str, str] = {}
    for consumer_id, digest in digests.items():
        if (
            not isinstance(consumer_id, str)
            or not consumer_id.strip()
            or not isinstance(digest, str)
            or not digest.strip()
            or re.fullmatch(r"[0-9a-f]{64}", digest.strip()) is None
        ):
            raise ConfigConflictError(
                "Gateway consumer health digest 必须包含非空 consumer_id/digest"
            )
        normalized[consumer_id.strip()] = digest.strip()
    return dict(sorted(normalized.items()))


def _consumer_health_digests_from_proof(
    health_proof: dict[str, object],
) -> dict[str, str]:
    raw_digests = health_proof.get("consumer_health_digests", {})
    if not isinstance(raw_digests, dict):
        raise ConfigConflictError(
            "Gateway health proof 的 consumer_health_digests 必须是对象"
        )
    return _normalize_consumer_health_digests(
        {str(consumer_id): digest for consumer_id, digest in raw_digests.items()}
    )


def _require_gateway_consumer_health_digests(
    digests: dict[str, str],
) -> dict[str, str]:
    normalized = _normalize_consumer_health_digests(digests)
    missing = REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS.difference(normalized)
    extra = set(normalized).difference(REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS)
    if missing or extra:
        raise ConfigConflictError(
            "Gateway health proof 必须包含完整 consumer 摘要集合: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return normalized


GatewayConfigRuntimeRollback = Callable[[], Awaitable[None]]
GatewayConfigRuntimeApplier = Callable[
    [GatewayConfig, GatewayConfig, str, Callable[[], None]],
    Awaitable[GatewayConfigRuntimeRollback | None],
]


class GatewayConfigReloadService:
    """Gateway 配置的候选、pending 和 active 管理器。"""

    _CONFIG_DOMAIN = "gateway"

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        config: GatewayConfig,
        config_path: Path,
        local_config_path: Path,
        schema_path: Path | None = None,
        on_runtime_config: GatewayConfigRuntimeApplier | None = None,
        gateway_id: str | None = None,
    ) -> None:
        self._state_store = state_store
        self._config = config
        self._config_path = config_path.expanduser().resolve()
        self._local_config_path = local_config_path.expanduser().resolve()
        self._schema_path = schema_path.expanduser().resolve() if schema_path else None
        self._on_runtime_config = on_runtime_config
        self._gateway_id = gateway_id
        self._watcher: ConfigFileWatcher | None = None
        now = datetime.now(timezone.utc)
        self._state_store.recover_expired_config_applies(
            config_domain=self._CONFIG_DOMAIN
        )
        active = state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        if active is not None and active.state != "active":
            raise ConfigConflictError(
                "Gateway active snapshot 当前需要恢复，禁止继续启动: "
                f"state={active.state}, error={active.last_error}"
            )

        pending = state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN
        )
        restart_intent = (
            state_store.get_gateway_restart_intent(candidate_id=pending.candidate_id)
            if pending is not None
            else None
        )
        if restart_intent is not None and restart_intent.state in {
            "active",
            "discarded",
        }:
            restart_intent = None
        visible_pending = (
            pending if pending is not None and pending.state != "active" else None
        )
        self._status = ConfigReloadStatus(
            healthy=True,
            revision=config.revision,
            last_success_at=now,
            last_attempt_at=now,
            last_error=None,
            state=(
                visible_pending.state
                if visible_pending is not None
                else "active"
            ),
            active_revision=active.active_revision if active is not None else None,
            pending_revision=(
                visible_pending.pending_revision
                if visible_pending is not None
                else None
            ),
            candidate_id=(
                visible_pending.candidate_id if visible_pending is not None else None
            ),
            candidate_ref=(
                restart_intent.candidate_ref if restart_intent is not None else None
            ),
            attempt_id=(
                visible_pending.last_attempt_id
                if visible_pending is not None
                else None
            ),
            apply_id=(
                visible_pending.last_apply_id if visible_pending is not None else None
            ),
        )

    def _assert_gateway_restart_intent_owner(self, intent) -> None:
        if self._gateway_id is None:
            return
        if intent.gateway_id is None:
            raise ConfigConflictError(
                "Gateway pending intent 缺少 gateway_id 绑定，必须重新生成 intent"
            )
        if intent.gateway_id != self._gateway_id:
            raise ConfigConflictError("Gateway pending intent 不属于当前 Gateway")

    @property
    def config(self) -> GatewayConfig:
        return self._config

    def status(self) -> ConfigReloadStatus:
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN
        )
        visible_pending = (
            pending if pending is not None and pending.state != "active" else None
        )
        if active is None and pending is None:
            return self._status
        state = visible_pending.state if visible_pending is not None else "active"
        restart_intent = (
            self._state_store.get_gateway_restart_intent(
                candidate_id=visible_pending.candidate_id
            )
            if visible_pending is not None
            else None
        )
        if restart_intent is not None and restart_intent.state in {
            "active",
            "discarded",
        }:
            restart_intent = None
        reason = self._status.reason
        if state == "discarded":
            reason = None
        elif state == "pending_restart":
            reason = "restart_required"
        elif state in {"conflict", "rejected", "recovery_required"}:
            reason = state
        return ConfigReloadStatus(
            healthy=(self._status.healthy or state == "discarded")
            and state
            not in {
                "conflict",
                "rejected",
                "recovery_required",
            },
            revision=(
                active.effective_digest if active is not None else self._status.revision
            ),
            last_success_at=self._status.last_success_at,
            last_attempt_at=self._status.last_attempt_at,
            last_error=(
                visible_pending.last_error
                if visible_pending is not None and visible_pending.last_error is not None
                else self._status.last_error
            ),
            restart_required=state == "pending_restart",
            reason=reason,
            changed_sections=self._status.changed_sections,
            state=state,
            active_revision=active.active_revision if active is not None else None,
            pending_revision=(
                visible_pending.pending_revision
                if visible_pending is not None
                else None
            ),
            candidate_id=(
                visible_pending.candidate_id if visible_pending is not None else None
            ),
            candidate_ref=(
                restart_intent.candidate_ref if restart_intent is not None else None
            ),
            attempt_id=(
                visible_pending.last_attempt_id
                if visible_pending is not None
                else None
            ),
            apply_id=(
                visible_pending.last_apply_id if visible_pending is not None else None
            ),
            layer_digests=active.layer_digests if active is not None else None,
            applied_paths=self._status.applied_paths,
            deferred_paths=self._status.deferred_paths,
        )

    def list_events(self, *, after: int = 0, limit: int = 100):
        return self._state_store.list_config_events(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
            limit=limit,
        )

    def claim_events_for_consumer(
        self,
        *,
        after: int,
        consumer_id: str,
        limit: int = 100,
    ):
        return self._state_store.claim_config_events_for_consumer(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
            consumer_id=consumer_id,
            limit=limit,
        )

    def mark_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ):
        return self._state_store.mark_config_event_delivered_for_consumer(
            event_id=event_id,
            consumer_id=consumer_id,
        )

    def ensure_event_cursor(self, *, after: int) -> None:
        self._state_store.ensure_config_event_cursor(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
        )

    def initialize_active_snapshot(self) -> None:
        baseline, source_generation, revisions, digests = self._source_baseline(
            self._config
        )
        payload = prepare_config_for_persistence(self._config.payload)
        if not isinstance(payload, dict):
            raise TypeError("Gateway 脱敏 active payload 必须是对象")
        self._state_store.ensure_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=revisions,
            layer_digests=digests,
            effective_digest=self._config.revision,
            schema_version=2,
            promoted_generation="gateway-bootstrap",
            secret_bindings=build_secret_binding_summary(self._config.payload),
        )

    async def _renew_apply_claim(self, apply_id: str, fencing_token: str) -> None:
        """在 Gateway 外部 apply 期间续租 claim，丢失时由 promotion CAS 拒绝。"""

        while True:
            await asyncio.sleep(10)
            await asyncio.to_thread(
                self._state_store.renew_config_apply_claim,
                config_domain=self._CONFIG_DOMAIN,
                apply_id=apply_id,
                fencing_token=fencing_token,
                lease_seconds=30,
            )

    async def start(self) -> None:
        if self._watcher is not None:
            raise RuntimeError("Gateway 配置监听器不允许重复启动")
        self.initialize_active_snapshot()
        watcher = ConfigFileWatcher(
            directories={self._config_path.parent, self._local_config_path.parent},
            candidate_paths={self._config_path, self._local_config_path},
            on_change=self.reload,
        )
        await watcher.start()
        self._watcher = watcher

    async def stop(self) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            await watcher.stop()

    async def reload(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            candidate = load_gateway_config(
                config_path=self._config_path,
                local_config_path=self._local_config_path,
                schema_path=self._schema_path,
                state_store=self._state_store,
            )
            if candidate.revision == self._config.revision:
                self._status = replace(
                    self._status,
                    healthy=True,
                    last_attempt_at=now,
                    state="active",
                    reason=None,
                    last_error=None,
                )
                return
            changed_paths = changed_json_paths(
                self._config.payload,
                candidate.payload,
                array_identity_keys=gateway_config_policy().array_identity_keys(),
            )
            baseline, source_generation, revisions, digests = self._source_baseline(
                candidate
            )
            payload = prepare_config_for_persistence(candidate.payload)
            if not isinstance(payload, dict):
                raise TypeError("Gateway 脱敏 candidate payload 必须是对象")
            candidate_id = f"candidate_{source_generation}_{candidate.revision}"
            active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
            pending = self._state_store.create_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=candidate_id,
                idempotency_key=f"reload:{source_generation}:{candidate.revision}",
                payload=payload,
                source_baseline=baseline,
                candidate_digest=candidate.revision,
                effective_digest=candidate.revision,
                target_generation="gateway-runtime",
                fencing_token=None,
                state="candidate_validated",
                base_active_revision=(
                    active.active_revision if active is not None else None
                ),
                source_generation=source_generation,
            )
            if pending.state in {"rejected", "conflict", "recovery_required"}:
                pending = self._state_store.update_pending_config_candidate_state(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    expected_state=pending.state,
                    state="candidate_validated",
                    last_error=None,
                )
            attempt_id = new_config_id("attempt")
            apply_id = new_config_id("apply")
            registry_revision = self._state_store.get_registry_revision()
            claim = self._state_store.begin_config_apply(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                owner="gateway-config-service",
                base_active_revision=active.active_revision
                if active is not None
                else None,
                target_generation="gateway-runtime",
                pending_revision=pending.pending_revision,
                source_baseline=pending.source_baseline,
                active_baseline=(active.source_baseline if active is not None else {}),
                registry_revision=registry_revision,
            )
            claim_renewal_task = asyncio.create_task(
                self._renew_apply_claim(claim.apply_id, claim.fencing_token)
            )
            def assert_apply_claim() -> None:
                self._state_store.assert_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )

            restart_paths = gateway_config_policy().restart_paths(changed_paths)
            if restart_paths:
                self._state_store.update_pending_config_candidate_state(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    expected_state="applying",
                    state="pending_restart",
                    last_error="Gateway 配置包含需要受控重启的运行时依赖",
                    event=ConfigEventInput(
                        event_id=f"config:{candidate_id}:restart_required",
                        config_domain=self._CONFIG_DOMAIN,
                        candidate_id=candidate_id,
                        attempt_id=attempt_id,
                        apply_id=apply_id,
                        idempotency_key=pending.idempotency_key,
                        commit_revision=None,
                        active_revision=None,
                        pending_revision=pending.pending_revision,
                        source="gateway-config-watcher",
                        result="restart_required",
                        activation_scope="restart_gateway",
                        changed_paths=changed_paths,
                        deferred_paths=changed_paths,
                        error="Gateway 配置包含需要受控重启的运行时依赖",
                    ),
                )
                active_runtime_generation = (
                    self._state_store.active_gateway_runtime_generation()
                )
                intent = self._state_store.request_gateway_restart(
                    candidate_ref=new_config_id("candidate_ref"),
                    candidate_id=candidate_id,
                    base_active_revision=(
                        active.active_revision if active is not None else None
                    ),
                    old_generation=(
                        (
                            active_runtime_generation.generation_id
                            if active_runtime_generation is not None
                            else active.promoted_generation
                        )
                        if active is not None
                        else None
                    ),
                    target_generation=new_config_id("gateway_generation"),
                    requested_by="gateway-config-watcher",
                    fencing_token=claim.fencing_token,
                    gateway_id=self._gateway_id,
                )
                self._status = replace(
                    self._status,
                    healthy=True,
                    revision=self._config.revision,
                    last_attempt_at=now,
                    last_error="Gateway 配置包含需要受控重启的运行时依赖",
                    restart_required=True,
                    reason="restart_required",
                    changed_sections=("gateway_runtime",),
                    state="pending_restart",
                    pending_revision=pending.pending_revision,
                    candidate_id=candidate_id,
                    candidate_ref=intent.candidate_ref,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    deferred_paths=changed_paths,
                )
                self._state_store.update_config_apply_journal(
                    apply_id=claim.apply_id,
                    expected_state="applying",
                    state="failed",
                    last_error="Gateway 配置等待受控重启",
                )
                claim_renewal_task.cancel()
                await asyncio.gather(claim_renewal_task, return_exceptions=True)
                self._state_store.release_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )
                return
            runtime_apply_started = False
            runtime_rollback: GatewayConfigRuntimeRollback | None = None
            try:
                if self._on_runtime_config is not None:
                    runtime_apply_started = True
                    runtime_rollback = await self._on_runtime_config(
                        candidate,
                        self._config,
                        claim.fencing_token,
                        assert_apply_claim,
                    )
                    self._state_store.append_config_apply_side_effect(
                        apply_id=claim.apply_id,
                        side_effect={
                            "resource": "gateway-runtime-config",
                            "action": "apply",
                            "candidate_id": candidate_id,
                            "changed_paths": list(changed_paths),
                        },
                    )
                active = self._state_store.get_active_config_snapshot(
                    self._CONFIG_DOMAIN
                )
                if active is None:
                    raise RuntimeError("Gateway active snapshot 在 promotion 前丢失")
                pending_layer_revisions = {
                    str(key): int(detail["layer_revision"])
                    for key, detail in pending.source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                pending_layer_digests = {
                    str(key): (
                        str(detail.get("layer_digest"))
                        if detail.get("layer_digest") is not None
                        else None
                    )
                    for key, detail in pending.source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                self._state_store.promote_active_config_snapshot(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=candidate_id,
                    payload=payload,
                    source_baseline=baseline,
                    source_generation=source_generation,
                    layer_revisions=revisions,
                    layer_digests=digests,
                    effective_digest=candidate.revision,
                    schema_version=2,
                    expected_active_revision=active.active_revision,
                    expected_pending_revision=pending.pending_revision,
                    expected_source_baseline=pending.source_baseline,
                    expected_source_generation=(
                        pending.source_generation
                        if pending.source_generation is not None
                        else source_generation
                    ),
                    expected_layer_revisions=pending_layer_revisions,
                    expected_layer_digests=pending_layer_digests,
                    expected_registry_revision=registry_revision,
                    expected_fencing_token=claim.fencing_token,
                    promoted_apply_id=claim.apply_id,
                    secret_bindings=build_secret_binding_summary(candidate.payload),
                    event=ConfigEventInput(
                        event_id=f"config:{candidate_id}:active",
                        config_domain=self._CONFIG_DOMAIN,
                        candidate_id=candidate_id,
                        attempt_id=attempt_id,
                        apply_id=apply_id,
                        idempotency_key=pending.idempotency_key,
                        commit_revision=None,
                        active_revision=None,
                        pending_revision=pending.pending_revision,
                        source="gateway-config-watcher",
                        result="applied",
                        activation_scope=gateway_config_policy().activation_scope_for(
                            changed_paths
                        ),
                        changed_paths=changed_paths,
                        applied_paths=changed_paths,
                    ),
                )
            except ConfigConflictError as error:
                (
                    recovery_required,
                    recovery_error,
                ) = await self._handle_runtime_apply_failure(
                    apply_id=claim.apply_id,
                    runtime_apply_started=runtime_apply_started,
                    runtime_rollback=runtime_rollback,
                    error=error,
                )
                error_detail = recovery_error or str(error)
                journal = self._state_store.get_config_apply_journal(
                    apply_id=claim.apply_id
                )
                recovery_required = recovery_required or bool(
                    journal and journal.side_effects and journal.state != "compensated"
                )
                if journal is not None and journal.state == "applying":
                    self._state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=error_detail,
                    )
                self._finish_gateway_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "conflict"),
                    result=("recovery_required" if recovery_required else "conflict"),
                    error=error_detail,
                    changed_paths=changed_paths,
                )
                raise
            except Exception as error:
                (
                    recovery_required,
                    recovery_error,
                ) = await self._handle_runtime_apply_failure(
                    apply_id=claim.apply_id,
                    runtime_apply_started=runtime_apply_started,
                    runtime_rollback=runtime_rollback,
                    error=error,
                )
                error_detail = recovery_error or str(error)
                journal = self._state_store.get_config_apply_journal(
                    apply_id=claim.apply_id
                )
                recovery_required = recovery_required or bool(
                    journal and journal.side_effects and journal.state != "compensated"
                )
                if journal is not None and journal.state == "applying":
                    self._state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=error_detail,
                    )
                self._finish_gateway_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "rejected"),
                    result=(
                        "recovery_required" if recovery_required else "apply_failed"
                    ),
                    error=error_detail,
                    changed_paths=changed_paths,
                )
                raise
            finally:
                claim_renewal_task.cancel()
                await asyncio.gather(claim_renewal_task, return_exceptions=True)
                self._state_store.release_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )
            self._config = candidate
            self._status = replace(
                self._status,
                healthy=True,
                revision=candidate.revision,
                last_success_at=now,
                last_attempt_at=now,
                last_error=None,
                restart_required=False,
                reason=None,
                changed_sections=tuple(
                    sorted(
                        {
                            path.strip("/").split("/")[0]
                            for path in changed_paths
                            if path != "/"
                        }
                    )
                ),
                state="active",
                candidate_id=None,
                candidate_ref=None,
                attempt_id=None,
                apply_id=None,
                applied_paths=changed_paths,
                deferred_paths=(),
            )
        except Exception as error:
            self._status = replace(
                self._status,
                healthy=False,
                last_attempt_at=now,
                last_error=f"{type(error).__name__}: {error}",
                reason=self._status.reason or "invalid_config",
            )
            raise

    async def _handle_runtime_apply_failure(
        self,
        *,
        apply_id: str,
        runtime_apply_started: bool,
        runtime_rollback: GatewayConfigRuntimeRollback | None,
        error: Exception,
    ) -> tuple[bool, str | None]:
        """在 active promotion 失败时补偿已经发生的 Gateway 运行时变更。"""

        if not runtime_apply_started:
            return False, None
        if runtime_rollback is None:
            self._state_store.update_config_apply_journal(
                apply_id=apply_id,
                expected_state="applying",
                state="recovery_required",
                last_error=(f"{error}; Gateway 运行时 apply 未提供可执行的回退句柄"),
            )
            return (
                True,
                f"{error}; Gateway 运行时 apply 未提供可执行的回退句柄",
            )

        self._state_store.update_config_apply_journal(
            apply_id=apply_id,
            expected_state="applying",
            state="recovery_required",
            last_error=str(error),
        )
        try:
            await runtime_rollback()
        except Exception as rollback_error:  # noqa: BLE001 - 回退必须捕获所有运行时故障
            self._state_store.record_config_apply_compensation(
                apply_id=apply_id,
                compensation={
                    "resource": "gateway-runtime-config",
                    "action": "rollback",
                    "status": "failed",
                    "error": str(rollback_error),
                },
            )
            return (
                True,
                (f"{error}; Gateway 运行时 apply 回退失败: {rollback_error}"),
            )
        self._state_store.record_config_apply_compensation(
            apply_id=apply_id,
            compensation={
                "resource": "gateway-runtime-config",
                "action": "rollback",
                "status": "succeeded",
            },
        )
        return False, None

    def load_pending_candidate(
        self,
        *,
        candidate_ref: str,
        gateway_id: str | None = None,
    ):
        """供受控新 Gateway generation 使用；ref 不匹配时绝不回退到 active。"""
        return self._state_store.load_gateway_pending_candidate(
            candidate_ref=candidate_ref,
            gateway_id=gateway_id,
        )

    def begin_pending_restart(self, *, candidate_ref: str):
        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway pending intent 已过期，必须显式重试"
            )
        self._assert_gateway_restart_intent_owner(intent)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None or pending.state not in {
            "pending_restart",
            "applying",
            "recovery_required",
        }:
            raise ConfigConflictError(
                "Gateway restart intent 绑定的 pending candidate 不存在或不可加载"
            )
        if intent.state in {"pending", "recovery_required"}:
            _, pending, _ = self._state_store.begin_gateway_restart_apply(
                candidate_ref=candidate_ref,
                attempt_id=new_config_id("attempt"),
                apply_id=new_config_id("apply"),
                owner="gateway-restart-supervisor",
            )
            return pending
        if intent.state != "applying":
            raise ConfigConflictError(
                f"Gateway restart intent 当前不可开始: state={intent.state}"
            )
        claim = self._state_store.get_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN
        )
        if claim is None or claim.candidate_id != intent.candidate_id:
            raise ConfigConflictError(
                "Gateway restart intent 已 applying，但缺少匹配 apply claim"
            )
        if claim.lease_expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway restart intent 的 apply claim 已过期，必须先进入 recovery_required"
            )
        return self.load_pending_candidate(
            candidate_ref=candidate_ref,
            gateway_id=self._gateway_id,
        )

    def record_pending_restart_failure(
        self,
        *,
        candidate_ref: str,
        target_generation: str,
        fencing_token: str,
        error: str,
    ) -> ConfigReloadStatus:
        """将失败的 Gateway pending 固定到恢复态，等待显式重试。"""

        if self._gateway_id is None:
            raise ConfigConflictError(
                "Gateway 启动失败回报缺少当前 Gateway identity"
            )
        record_gateway_restart_startup_failure(
            state_store=self._state_store,
            candidate_ref=candidate_ref,
            error=error,
            gateway_id=self._gateway_id,
            target_generation=target_generation,
            fencing_token=fencing_token,
        )
        return self.status()

    def retry_pending_restart(
        self,
        *,
        candidate_ref: str,
        requested_by: str = "gateway-restart-retry",
    ) -> ConfigReloadStatus:
        """为恢复态 pending 生成新的 generation/token，等待下一次受控重启。"""

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        self._state_store.retry_gateway_restart(
            candidate_ref=candidate_ref,
            target_generation=new_config_id("gateway_generation"),
            requested_by=requested_by,
        )
        return self.status()

    def discard_pending_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
    ) -> ConfigReloadStatus:
        """仅在旧 Gateway active/source 基线安全时丢弃 pending。"""

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None:
            raise ConfigConflictError(
                "Gateway restart intent 绑定的 pending candidate 不存在"
            )
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        if active is None:
            raise ConfigConflictError("Gateway pending discard 缺少 active snapshot")
        changed_paths = changed_json_paths(active.payload, pending.payload)
        self._state_store.discard_gateway_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
            expected_source_baseline=pending.source_baseline,
            reason="用户显式丢弃 Gateway pending candidate",
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:discarded",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=pending.last_apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision,
                pending_revision=pending.pending_revision,
                source="gateway-config-api",
                result="discarded",
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
            ),
        )
        return self.status()

    def resolve_pending_restart(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
    ) -> ConfigReloadStatus:
        """使用匹配的 Gateway proof 完成 recovery_required promotion。"""

        self.record_pending_restart_proof(
            candidate_ref=candidate_ref,
            health_proof=health_proof,
        )
        return self.status()

    def record_pending_restart_proof(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
        runtime_generation_id: str | None = None,
        old_generation_id: str | None = None,
    ):
        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        if not health_proof.get("generation") or not health_proof.get("health_digest"):
            raise ValueError(
                "Gateway health proof 必须包含 generation 和 health_digest"
            )
        if health_proof["generation"] != intent.target_generation:
            raise ConfigConflictError("Gateway health proof generation 不匹配")
        consumer_health_digests = _require_gateway_consumer_health_digests(
            _consumer_health_digests_from_proof(health_proof)
        )
        expected_proof = self.build_pending_restart_health_proof(
            candidate_ref=candidate_ref,
            generation=intent.target_generation,
            consumer_health_digests=consumer_health_digests,
        )
        if health_proof != expected_proof:
            raise ConfigConflictError(
                "Gateway health proof 与 pending candidate 不匹配"
            )
        runtime_generation_id = runtime_generation_id or str(health_proof["generation"])
        old_generation_id = (
            old_generation_id
            if old_generation_id is not None
            else intent.old_generation
        )
        pending = self.begin_pending_restart(candidate_ref=candidate_ref)
        claim = self._state_store.get_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN
        )
        if claim is None or claim.candidate_id != pending.candidate_id:
            raise ConfigConflictError("Gateway pending promotion 缺少匹配 apply claim")
        journal = self._state_store.get_config_apply_journal(apply_id=claim.apply_id)
        if journal is None:
            raise ConfigConflictError("Gateway pending promotion 缺少 apply journal")
        baseline = pending.source_baseline
        if not isinstance(baseline, dict):
            raise TypeError("Gateway pending source baseline 必须是对象")
        layer_revisions: dict[str, int] = {}
        layer_digests: dict[str, str | None] = {}
        source_generation = 0
        for key, raw_detail in baseline.items():
            if not isinstance(key, str) or not isinstance(raw_detail, dict):
                raise TypeError("Gateway pending source baseline 结构无效")
            raw_revision = raw_detail.get("layer_revision")
            if raw_revision is not None:
                layer_revisions[key] = int(raw_revision)
            raw_digest = raw_detail.get("layer_digest")
            layer_digests[key] = str(raw_digest) if raw_digest is not None else None
            raw_generation = raw_detail.get("source_generation")
            if raw_generation is not None:
                source_generation = max(source_generation, int(raw_generation))
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        changed_paths = (
            changed_json_paths(active.payload, pending.payload)
            if active is not None
            else ()
        )
        promoted = self._state_store.promote_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            payload=prepare_config_for_persistence(pending.payload),
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=pending.effective_digest,
            schema_version=2,
            expected_active_revision=(
                active.active_revision if active is not None else None
            ),
            expected_pending_revision=pending.pending_revision,
            expected_source_baseline=baseline,
            expected_source_generation=(
                pending.source_generation
                if pending.source_generation is not None
                else source_generation
            ),
            expected_layer_revisions=layer_revisions,
            expected_layer_digests=layer_digests,
            expected_registry_revision=journal.registry_revision,
            expected_pending_state="applying",
            expected_fencing_token=claim.fencing_token,
            promoted_apply_id=claim.apply_id,
            secret_bindings=build_secret_binding_summary(
                pending.payload,
                resolve_environment=True,
            ),
            promoted_generation=intent.target_generation,
            gateway_candidate_ref=candidate_ref,
            gateway_health_proof=health_proof,
            gateway_runtime_generation_id=runtime_generation_id,
            gateway_old_generation_id=old_generation_id,
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:active",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=claim.apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=None,
                pending_revision=pending.pending_revision,
                source="gateway-restart-supervisor",
                result="applied",
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
                applied_paths=changed_paths,
            ),
        )
        self._status = replace(
            self._status,
            healthy=True,
            revision=pending.effective_digest,
            last_success_at=datetime.now(timezone.utc),
            last_error=None,
            restart_required=False,
            reason=None,
            state="active",
            active_revision=promoted.active_revision,
            pending_revision=None,
            candidate_id=None,
            candidate_ref=None,
            attempt_id=None,
            apply_id=None,
            applied_paths=(),
            deferred_paths=(),
        )
        self._state_store.release_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN,
            apply_id=claim.apply_id,
            fencing_token=claim.fencing_token,
        )
        return promoted

    def build_pending_restart_health_proof(
        self,
        *,
        candidate_ref: str,
        generation: str,
        consumer_health_digests: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """构造不含秘密的 Gateway 自身健康证明。"""

        if self._gateway_id is None:
            raise ConfigConflictError(
                "Gateway health proof 缺少 gateway_id，不能用于 pending promotion"
            )

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway pending intent 已过期，必须显式重试"
            )
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None or pending.state not in {
            "pending_restart",
            "applying",
            "recovery_required",
        }:
            raise ConfigConflictError(
                "Gateway health proof 缺少可证明的 pending candidate"
            )
        if intent.state not in {"pending", "applying", "recovery_required"}:
            raise ConfigConflictError(
                "Gateway health proof 当前不允许用于该 restart intent: "
                f"state={intent.state}"
            )
        if pending.target_generation != generation:
            raise ConfigConflictError("Gateway health proof generation 不匹配 pending")
        if pending.fencing_token != intent.fencing_token:
            raise ConfigConflictError("Gateway health proof fencing token 不匹配")
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        secret_bindings = build_secret_binding_summary(
            pending.payload,
            resolve_environment=True,
        )
        secret_binding_digest = hashlib.sha256(
            json.dumps(
                secret_bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fencing_token_digest = hashlib.sha256(
            intent.fencing_token.encode("utf-8")
        ).hexdigest()
        proof_payload = {
            "config_domain": "gateway",
            "candidate_ref": candidate_ref,
            "candidate_id": pending.candidate_id,
            "generation": generation,
            "loaded_source": "pending",
            "active_revision": active.active_revision if active is not None else None,
            "pending_revision": pending.pending_revision,
            "candidate_digest": pending.candidate_digest,
            "effective_digest": pending.effective_digest,
            "secret_binding_digest": secret_binding_digest,
            "fencing_token_digest": fencing_token_digest,
            "consumer_health_digests": _require_gateway_consumer_health_digests(
                consumer_health_digests or {}
            ),
        }
        if self._gateway_id is not None:
            proof_payload["gateway_id"] = self._gateway_id
        health_digest = hashlib.sha256(
            json.dumps(
                proof_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {**proof_payload, "health_digest": health_digest}

    def _finish_gateway_candidate(
        self,
        pending,
        *,
        attempt_id: str,
        apply_id: str,
        state,
        result,
        error: str,
        changed_paths: tuple[str, ...],
    ) -> None:
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        self._state_store.update_pending_config_candidate_state(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            expected_state="applying",
            state=state,
            last_error=error,
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:{result}",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision if active is not None else None,
                pending_revision=pending.pending_revision,
                source="gateway-config-watcher",
                result=result,
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
                error=error,
            ),
        )

    def _source_baseline(
        self,
        config: GatewayConfig,
    ) -> tuple[dict[str, object], int, dict[str, int], dict[str, str | None]]:
        baseline: dict[str, object] = {}
        revisions: dict[str, int] = {}
        digests: dict[str, str | None] = {}
        source_generation = 0
        for source in config.source_details:
            key = source.source_key or f"{source.layer}:{source.precedence}"
            source_path = source.path
            stored_source = self._state_store.get_source_layer(key)
            if stored_source is not None:
                source_path = Path(stored_source.source_path)
            baseline[key] = {
                "path": str(source_path),
                "presence": source.presence,
                "layer_revision": source.layer_revision,
                "layer_digest": source.layer_digest,
                "source_generation": source.source_generation,
            }
            if source.layer_revision is not None:
                revisions[key] = source.layer_revision
            digests[key] = source.layer_digest
            if source.source_generation is not None:
                source_generation = max(source_generation, source.source_generation)
        return baseline, source_generation, revisions, digests


def _load_or_migrate_gateway_override(
    *,
    state_store: GatewayStateStore,
    config_key: str,
    path: Path,
    persist_migrations: bool,
) -> dict[str, object] | None:
    if persist_migrations:
        blocked = state_store.migrate_legacy_config_secrets(config_key)
        if blocked:
            raise SecretReferenceRequiredError(
                "旧 Gateway SQLite secret 必须重新导入引用: " + ", ".join(blocked)
            )
    record = state_store.get_config(config_key)
    source_record = state_store.get_source_layer(config_key)
    file_snapshot = read_stable_config_file(path)
    if file_snapshot.presence == "absent":
        if persist_migrations and (source_record is not None or record is not None):
            deleted_backup_path = path.with_name(f"{path.name}.deleted.bak")
            if not deleted_backup_path.exists() and record is not None:
                deleted_backup_path.write_text(
                    dump_json(redact_config_payload(record.payload)),
                    encoding="utf-8",
                )
            verify_stable_config_file(file_snapshot)
            source_record = state_store.sync_config_source(
                config_key=config_key,
                source_path=path,
                config_version=(
                    source_record.config_version
                    if source_record is not None
                    else record.config_version
                    if record is not None
                    else 1
                ),
                presence="absent",
                payload=None,
                layer_digest=None,
                expected_layer_revision=(
                    source_record.layer_revision if source_record is not None else None
                ),
                expected_layer_digest=(
                    source_record.layer_digest if source_record is not None else None
                ),
                backup_path=deleted_backup_path if record is not None else None,
                journal_origin="file-watcher",
            )
            _record_gateway_source_journal(
                state_store, source_record, origin="file-watcher"
            )
        # 删除文件的语义优先于旧 SQLite payload，不能被旧记录遮蔽。
        return None
    if (
        source_record is not None
        and source_record.presence == "present"
        and source_record.layer_digest == file_snapshot.digest
        and source_record.payload is not None
    ):
        _record_gateway_source_journal(state_store, source_record, origin="loader")
        payload = parse_stable_config_file(file_snapshot)
        if payload is None:
            raise RuntimeError(f"Gateway 配置文件解析为空: {path}")
        verify_stable_config_file(file_snapshot)
        source_record = state_store.sync_config_source(
            config_key=config_key,
            source_path=path,
            config_version=int(payload.get("config_version", 1)),
            presence="present",
            payload=payload,
            layer_digest=file_snapshot.digest,
            expected_layer_revision=source_record.layer_revision,
            expected_layer_digest=source_record.layer_digest,
            journal_origin="loader",
        )
        _record_gateway_source_journal(state_store, source_record, origin="loader")
        return payload
    if not persist_migrations:
        payload = parse_stable_config_file(file_snapshot)
        if payload is None:
            raise RuntimeError(f"Gateway 配置文件解析为空: {path}")
        return payload
    payload = parse_stable_config_file(file_snapshot)
    if payload is None:
        raise RuntimeError(f"Gateway 配置文件解析为空: {path}")
    backup_path = path.with_name(f"{path.name}.migrated.bak")
    verify_stable_config_file(file_snapshot)
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    source_record = state_store.sync_config_source(
        config_key=config_key,
        config_version=int(payload.get("config_version", 1)),
        source_path=path,
        presence="present",
        payload=payload,
        layer_digest=file_snapshot.digest,
        expected_layer_revision=(
            source_record.layer_revision if source_record is not None else None
        ),
        expected_layer_digest=(
            source_record.layer_digest if source_record is not None else None
        ),
        backup_path=backup_path,
        journal_origin="file-watcher",
    )
    if source_record.payload is None:
        raise RuntimeError(f"Gateway source layer 缺少 payload: {config_key}")
    _record_gateway_source_journal(state_store, source_record, origin="file-watcher")
    return dict(source_record.payload)


def _record_gateway_source_journal(
    state_store: GatewayStateStore,
    source_record,
    *,
    origin: str,
) -> None:
    state_store.append_config_source_journal(
        source_key=source_record.config_key,
        source_event_id=(
            f"{source_record.config_key}:layer:{source_record.layer_revision}"
        ),
        source_path=Path(source_record.source_path),
        presence=source_record.presence,
        layer_revision=source_record.layer_revision,
        layer_digest=source_record.layer_digest,
        previous_digest=source_record.previous_digest,
        origin=origin,
        fanout_id=(f"fanout:{source_record.config_key}:{source_record.layer_revision}"),
        expected_source_generation=state_store.source_generation_high_water_mark(
            source_key=source_record.config_key
        ),
    )


def _configured_theme_background(
    background: dict[str, object] | None,
    *,
    config_root: Path,
) -> dict[str, object] | None:
    if background is None or background.get("type") != "local_file":
        return background
    return {
        **background,
        "path": str(
            resolve_gateway_path(cast(str, background["path"]), config_root=config_root)
        ),
    }


def resolve_gateway_path(value: str, *, config_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((config_root or get_user_config_root()) / raw_path).resolve()
