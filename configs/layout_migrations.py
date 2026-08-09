from __future__ import annotations

import hashlib
import json
from pathlib import Path

import commentjson
import jsonschema

from configs.installer import atomic_write
from configs.legacy_migrations import read_and_migrate_config

_LAYOUT_STATE_NAME = ".config-layout-migration.json"


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_schema(path: Path) -> dict[str, object]:
    parsed = commentjson.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(f"配置 schema 根节点必须是对象: {path}")
    return parsed


def _validate_candidate(
    payload: dict[str, object],
    *,
    schema_path: Path,
    source_path: Path,
) -> None:
    try:
        jsonschema.validate(payload, _load_schema(schema_path))
    except jsonschema.ValidationError as error:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ValueError(
            f"旧配置拆分后的候选无效: source={source_path} "
            f"schema={schema_path} location={location}: {error.message}"
        ) from error


def _commit_split_layout(
    *,
    legacy_path: Path,
    targets: tuple[tuple[Path, bytes], ...],
) -> bool:
    state_path = legacy_path.parent / _LAYOUT_STATE_NAME
    source_digest = _digest(legacy_path.read_bytes())
    expected_state = {
        "schema_version": 1,
        "legacy_path": legacy_path.name,
        "source_sha256": source_digest,
        "targets": {path.name: _digest(content) for path, content in targets},
    }
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state != expected_state:
            raise RuntimeError(f"配置布局迁移状态与旧配置不匹配: {state_path}")
    else:
        existing_targets = [str(path) for path, _ in targets if path.exists()]
        if existing_targets:
            raise RuntimeError(
                "新旧配置布局同时存在，拒绝猜测优先级: "
                f"legacy={legacy_path} targets={existing_targets}"
            )
        atomic_write(state_path, _json_bytes(expected_state), 0o600)

    for target_path, content in targets:
        if target_path.exists():
            if _digest(target_path.read_bytes()) != _digest(content):
                raise RuntimeError(f"配置布局迁移目标被修改，拒绝覆盖: {target_path}")
            continue
        atomic_write(target_path, content, 0o600)

    for target_path, content in targets:
        if not target_path.is_file() or _digest(target_path.read_bytes()) != _digest(
            content
        ):
            raise RuntimeError(f"配置布局迁移目标校验失败: {target_path}")
    legacy_path.unlink()
    state_path.unlink()
    return True


def migrate_legacy_user_configuration(
    *,
    config_root: Path,
    gateway_schema_path: Path,
    workspace_schema_path: Path,
) -> bool:
    resolved_root = config_root.expanduser().resolve()
    legacy_path = resolved_root / "boxteam.jsonc"
    if not legacy_path.exists():
        return False
    legacy = dict(read_and_migrate_config(legacy_path, persist=False).config)
    raw_gateway = legacy.pop("gateway", {"workspaces": []})
    if not isinstance(raw_gateway, dict):
        raise TypeError(f"旧配置 gateway 必须是对象: {legacy_path}")
    gateway = {
        "$schema": "./gateway_schema.jsonc",
        "config_version": 1,
        **raw_gateway,
    }
    legacy.pop("$schema", None)
    legacy.pop("config_version", None)
    workspace = {
        "$schema": "./workspace_schema.jsonc",
        "config_version": 1,
        **legacy,
    }
    _validate_candidate(
        gateway,
        schema_path=gateway_schema_path,
        source_path=legacy_path,
    )
    _validate_candidate(
        workspace,
        schema_path=workspace_schema_path,
        source_path=legacy_path,
    )
    return _commit_split_layout(
        legacy_path=legacy_path,
        targets=(
            (resolved_root / "gateway.jsonc", _json_bytes(gateway)),
            (resolved_root / "workspace.jsonc", _json_bytes(workspace)),
        ),
    )


def migrate_legacy_workspace_configuration(
    *,
    workspace_root: Path,
    workspace_schema_path: Path,
) -> bool:
    config_root = workspace_root.expanduser().resolve() / ".boxteam"
    legacy_path = config_root / "boxteam.jsonc"
    if not legacy_path.exists():
        return False
    legacy = dict(read_and_migrate_config(legacy_path, persist=False).config)
    if "gateway" in legacy:
        raise ValueError(
            "工作区级旧配置包含 gateway 字段；Gateway 配置只能迁移到 "
            f"BOXTEAM_HOME/config/gateway.jsonc: {legacy_path}"
        )
    legacy.pop("$schema", None)
    legacy.pop("config_version", None)
    workspace = {
        "$schema": "./workspace_schema.jsonc",
        "config_version": 1,
        **legacy,
    }
    _validate_candidate(
        workspace,
        schema_path=workspace_schema_path,
        source_path=legacy_path,
    )
    return _commit_split_layout(
        legacy_path=legacy_path,
        targets=((config_root / "workspace.jsonc", _json_bytes(workspace)),),
    )
