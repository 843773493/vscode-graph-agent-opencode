"""显式日志迁移的只读计划；不通过运行时 loader 初始化或改变配置状态。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config_sources import parse_stable_config_file, read_stable_config_file
from app.core.logging_contract import RuntimeLogContract, load_runtime_log_contract
from app.services.infrastructure.config.state import CONFIG_LIFECYCLE_STATES

ConfigDomain = Literal["gateway", "workspace"]
TARGET_CONFIG_VERSIONS = {"gateway": 3, "workspace": 2}
_SOURCE_KEYS: dict[ConfigDomain, tuple[str, ...]] = {
    "gateway": ("gateway_mutable_override", "gateway_local_mutable_override"),
    "workspace": (
        "workspace_mutable_override", "workspace_local_mutable_override",
        "workspace_root_mutable_override",
    ),
}


@dataclass(frozen=True, slots=True)
class LoggingMigrationInput:
    domain: ConfigDomain
    path: Path
    kind: Literal["file", "source_layer", "active"]
    key: str
    payload: dict[str, object] | None
    digest: str | None
    revision: int | None = None
    source_config_version: int | None = None

    @property
    def identity(self) -> str:
        return f"{self.path}#{self.kind}:{self.key}"

    @property
    def config_version(self) -> object:
        if self.source_config_version is not None:
            return self.source_config_version
        return self.payload.get("config_version", 1) if self.payload is not None else None

    def settings(self, contract: RuntimeLogContract) -> dict[str, str | bool]:
        payload = self.payload or {}
        if self.domain == "workspace":
            if "logging" in payload:
                raise ValueError(f"Workspace 不能包含 Gateway logging 配置: {self.identity}")
            value = payload.get("logger", {})
        else:
            logging = payload.get("logging", {})
            if not isinstance(logging, dict):
                raise TypeError(f"logging 必须是对象: {self.identity}")
            value = logging.get("runtime", {})
        if not isinstance(value, dict) or set(value) - {"level", "pretty"}:
            raise ValueError(f"日志设置必须是仅含 level/pretty 的对象: {self.identity}")
        return _validated_settings(value, contract=contract, source=self.identity)


def _validated_settings(
    value: dict[str, object], *, contract: RuntimeLogContract, source: str,
) -> dict[str, str | bool]:
    settings: dict[str, str | bool] = {}
    if "level" in value:
        level = value["level"]
        if not isinstance(level, str) or level.strip().lower() not in contract.levels:
            raise ValueError(f"日志 level 非法: {source}")
        settings["level"] = level.strip().lower()
    if "pretty" in value:
        if not isinstance(value["pretty"], bool):
            raise TypeError(f"日志 pretty 必须是布尔值: {source}")
        settings["pretty"] = value["pretty"]
    return settings


@dataclass(frozen=True, slots=True)
class RuntimeLoggingMigrationPlan:
    inputs: tuple[LoggingMigrationInput, ...]
    settings: dict[str, str | bool] | None
    conflicts: tuple[dict[str, object], ...]
    unsettled: tuple[dict[str, str], ...]
    choices: dict[str, str | bool]
    contract: RuntimeLogContract

    def report(self) -> dict[str, object]:
        # 只输出日志字段与来源身份，不能把模型密钥或完整业务配置带入预览。
        return {
            "validation_stage": "logging_preflight",
            "target_config_versions": dict(TARGET_CONFIG_VERSIONS),
            "settings": self.settings,
            "preserve_gateway_layers": self.settings is None,
            "explicit_choices": self.choices,
            "conflicts": list(self.conflicts),
            "unsettled": list(self.unsettled),
            "sources": [
                {
                    "source": item.identity, "domain": item.domain,
                    "presence": "present" if item.payload is not None else "absent",
                    "config_version": item.config_version,
                    "digest": item.digest, "revision": item.revision,
                    "legacy_logger": item.domain == "workspace" and "logger" in (item.payload or {}),
                    "settings": item.settings(self.contract),
                }
                for item in self.inputs
            ],
            "format_notice": "写入阶段将格式化改动的 JSONC，并备份其原始字节；当前预览不写入。",
        }


def _json_payload(content: str, *, source: str) -> dict[str, object]:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise TypeError(f"SQLite 配置正文必须是对象: {source}")
    return payload


def _read_database(
    path: Path, domain: ConfigDomain,
) -> tuple[list[LoggingMigrationInput], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    inputs: list[LoggingMigrationInput] = []
    unsettled: list[dict[str, str]] = []
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        for key, presence, content, revision, digest, version in connection.execute(
            "SELECT config_key, presence, payload_json, layer_revision, layer_digest, config_version FROM config_source_layers ORDER BY config_key"
        ):
            if key not in _SOURCE_KEYS[domain]:
                continue
            if presence not in {"present", "absent"} or (presence == "present") != (content is not None):
                raise ValueError(f"SQLite 配置来源存在性与正文不一致: {path}#{key}")
            inputs.append(LoggingMigrationInput(
                domain, path, "source_layer", key,
                _json_payload(content, source=f"{path}#{key}") if content is not None else None,
                digest, revision, version,
            ))
        for content, revision, state, digest in connection.execute(
            "SELECT payload_json, active_revision, state, effective_digest FROM config_active_snapshot WHERE config_domain = ?",
            (domain,),
        ):
            inputs.append(LoggingMigrationInput(
                domain, path, "active", domain,
                _json_payload(content, source=f"{path}#active"),
                digest, revision,
            ))
            if state not in CONFIG_LIFECYCLE_STATES:
                raise ValueError(f"SQLite 配置 active 状态非法: {path}#{domain}")
            if state != "active":
                unsettled.append({"source": str(path), "kind": "active", "id": domain, "state": state})
        for candidate_id, state in connection.execute(
            "SELECT candidate_id, state FROM config_pending_candidate WHERE config_domain = ? ORDER BY candidate_id",
            (domain,),
        ):
            if state not in CONFIG_LIFECYCLE_STATES:
                raise ValueError(f"SQLite 配置候选状态非法: {path}#{candidate_id}")
            if state not in {"active", "rejected", "discarded"}:
                unsettled.append({"source": str(path), "kind": "candidate", "id": candidate_id, "state": state})
        for apply_id, state in connection.execute(
            "SELECT apply_id, state FROM config_apply_journal WHERE config_domain = ? ORDER BY apply_id",
            (domain,),
        ):
            if state not in {"applying", "committed", "failed", "recovery_required", "compensated"}:
                raise ValueError(f"SQLite 配置应用状态非法: {path}#{apply_id}")
            if state in {"applying", "recovery_required"}:
                unsettled.append({"source": str(path), "kind": "apply", "id": apply_id, "state": state})
        for apply_id in connection.execute(
            "SELECT apply_id FROM config_apply_claim WHERE config_domain = ?", (domain,),
        ):
            unsettled.append({"source": str(path), "kind": "claim", "id": apply_id[0], "state": "claimed"})
    finally:
        connection.close()
    return inputs, unsettled


def preview_runtime_logging_migration(
    *, boxteam_home: Path, workspaces: tuple[Path, ...] = (),
    level: str | None = None, pretty: bool | None = None,
    project_root: Path | None = None, gateway_sqlite_path: Path | None = None,
) -> RuntimeLoggingMigrationPlan:
    """读取显式输入；不扫描工作区、不打开可写 store、不改变 active/pending。"""
    contract = load_runtime_log_contract(project_root)
    choices = _validated_settings(
        {**({"level": level} if level is not None else {}), **({"pretty": pretty} if pretty is not None else {})},
        contract=contract, source="迁移命令显式选择",
    )
    home = boxteam_home.expanduser().resolve()
    files: list[tuple[ConfigDomain, Path]] = [
        ("gateway", home / "config/gateway.jsonc"),
        ("gateway", home / "config/gateway_local.jsonc"),
        ("workspace", home / "config/workspace.jsonc"),
        ("workspace", home / "config/workspace_local.jsonc"),
    ]
    databases: list[tuple[ConfigDomain, Path]] = [
        ("gateway", gateway_sqlite_path.expanduser().resolve() if gateway_sqlite_path is not None else home / "state/gateway/gateway.sqlite"),
    ]
    legacy_files = [home / "config/boxteam.jsonc"]
    for root in dict.fromkeys(path.expanduser().resolve() for path in workspaces):
        if not root.is_dir():
            raise NotADirectoryError(f"显式迁移工作区不存在: {root}")
        files.append(("workspace", root / ".boxteam/workspace.jsonc"))
        databases.append(("workspace", root / ".boxteam/state/workspace.sqlite"))
        legacy_files.append(root / ".boxteam/boxteam.jsonc")
    inputs: list[LoggingMigrationInput] = []
    unsettled: list[dict[str, str]] = []
    for path in legacy_files:
        if path.exists():
            unsettled.append({"source": str(path), "kind": "legacy_layout", "id": path.name, "state": "migration_required"})
    for domain, path in files:
        snapshot = read_stable_config_file(path)
        inputs.append(LoggingMigrationInput(
            domain, path, "file", path.name, parse_stable_config_file(snapshot), snapshot.digest,
        ))
    for domain, path in databases:
        stored, pending = _read_database(path, domain)
        inputs.extend(stored)
        unsettled.extend(pending)
    observed: dict[str, list[tuple[str, str | bool]]] = {"level": [], "pretty": []}
    for item in inputs:
        versions = [item.config_version] if item.payload is not None else []
        if item.payload is not None and "config_version" in item.payload:
            versions.append(item.payload["config_version"])
        for version in versions:
            if type(version) is not int or not 1 <= version <= TARGET_CONFIG_VERSIONS[item.domain]:
                raise ValueError(f"配置版本不在日志迁移支持范围: {item.identity}")
        for field, value in item.settings(contract).items():
            observed[field].append((item.identity, value))
    has_legacy = any(item.domain == "workspace" and "logger" in (item.payload or {}) for item in inputs)
    conflicts: list[dict[str, object]] = []
    settings: dict[str, str | bool] | None = None
    if has_legacy or choices:
        settings = {"level": contract.default_level, "pretty": contract.default_pretty}
        for field, values in observed.items():
            unique = {value for _, value in values}
            if field in choices:
                settings[field] = choices[field]
            elif len(unique) > 1:
                settings.pop(field)
                conflicts.append({"field": field, "sources": [{"source": source, "value": value} for source, value in values]})
            elif values:
                settings[field] = values[0][1]
    return RuntimeLoggingMigrationPlan(tuple(inputs), settings, tuple(conflicts), tuple(unsettled), choices, contract)
