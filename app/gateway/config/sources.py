from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from app.core.config_sources import (
    ConfigSource,
    ConfigSourceLayer,
    ConfigSourcePresence,
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config.state import (
    SecretReferenceRequiredError,
    dump_json,
    redact_config_payload,
)

from .values import _GATEWAY_SOURCE_LAYER_AUTHORITY


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
def _gateway_source_detail(
    state_store: GatewayStateStore,
    config_key: str,
    *,
    fallback_path: Path,
) -> ConfigSource:
    """返回一条 Gateway 来源的 VRN 兄弟字段形态（有 state store 路径）。

    逻辑来源层与 precedence 一律取自 `_GATEWAY_SOURCE_LAYER_AUTHORITY`，本函数 MUST NOT
    硬编码 `sqlite`：有 store 时 `gateway_mutable_override`/`gateway_local_mutable_override`
    虽共享同一 `gateway.sqlite`，但共享只是承载事实，MUST NOT 有损改写层名。
    """
    record = state_store.get_source_layer(config_key)
    layer, precedence = _GATEWAY_SOURCE_LAYER_AUTHORITY[config_key]
    # 不可寻址（共享 gateway.sqlite，且 real path 不持久化），vrn=None。
    return ConfigSource(
        vrn=None,
        layer=layer,
        precedence=precedence,
        loaded=(
            record.presence == "present"
            if record is not None
            else fallback_path.is_file()
        ),
        source_key=config_key,
        presence=(
            record.presence
            if record is not None
            else "present"
            if fallback_path.is_file()
            else "absent"
        ),
        layer_revision=record.layer_revision if record is not None else None,
        layer_digest=record.layer_digest if record is not None else None,
        source_generation=record.source_generation if record is not None else None,
    )


def _gateway_persisted_source_details(
    source_baseline: dict[str, object],
) -> tuple[ConfigSource, ...]:
    """从 active/pending baseline 恢复逻辑来源，不把快照本身当作来源。"""

    if not source_baseline:
        raise ValueError("Gateway active/pending snapshot 来源基线不能为空")
    details: list[ConfigSource] = []
    for source_key, raw_value in sorted(source_baseline.items()):
        if not isinstance(raw_value, dict):
            raise TypeError(f"Gateway source baseline 必须是对象: key={source_key}")
        raw_value = cast(dict[str, object], raw_value)
        raw_vrn = raw_value.get("vrn")
        if raw_vrn is not None and (not isinstance(raw_vrn, str) or not raw_vrn):
            raise ValueError(
                f"Gateway source baseline vrn 必须是非空字符串或 None: key={source_key}"
            )
        presence_value = raw_value.get("presence")
        if presence_value == "present":
            presence: ConfigSourcePresence = "present"
        elif presence_value == "absent":
            presence = "absent"
        else:
            raise ValueError(
                f"Gateway source baseline presence 无效: key={source_key}"
            )
        layer, precedence = _resolve_gateway_persisted_layer(source_key)
        details.append(
            ConfigSource(
                vrn=raw_vrn,
                layer=layer,
                precedence=precedence,
                loaded=presence == "present",
                source_key=source_key,
                presence=presence,
                layer_revision=(
                    int(raw_value["layer_revision"])
                    if raw_value.get("layer_revision") is not None
                    else None
                ),
                layer_digest=(
                    str(raw_value["layer_digest"])
                    if raw_value.get("layer_digest") is not None
                    else None
                ),
                source_generation=(
                    int(raw_value["source_generation"])
                    if raw_value.get("source_generation") is not None
                    else None
                ),
            )
        )
    if not any(source.layer == "inline" for source in details):
        raise ValueError("Gateway active/pending snapshot 来源基线缺少 inline 来源")
    details.sort(key=lambda source: source.precedence)
    return tuple(details)


def _resolve_gateway_persisted_layer(
    source_key: str,
) -> tuple[ConfigSourceLayer, int]:
    """只从 Gateway owner authority 还原持久来源层，未登记键必须失败。"""

    if source_key in _GATEWAY_SOURCE_LAYER_AUTHORITY:
        return _GATEWAY_SOURCE_LAYER_AUTHORITY[source_key]
    raise ValueError(
        "Gateway source baseline 含未登记的持久化键，无法还原逻辑来源层: "
        f"key={source_key!r}"
    )


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
                "旧 Gateway SQLite 含无法恢复的秘密摘要，必须重新导入: "
                + ", ".join(blocked)
            )
    source_record = state_store.get_source_layer(config_key)
    file_snapshot = read_stable_config_file(path)
    if file_snapshot.presence == "absent":
        if persist_migrations and source_record is not None:
            deleted_backup_path = path.with_name(f"{path.name}.deleted.bak")
            if not deleted_backup_path.exists() and source_record.payload is not None:
                deleted_backup_path.write_text(
                    dump_json(redact_config_payload(source_record.payload)),
                    encoding="utf-8",
                )
            verify_stable_config_file(file_snapshot)
            source_record = state_store.sync_config_source(
                config_key=config_key,
                vrn=None,
                config_version=source_record.config_version,
                presence="absent",
                payload=None,
                layer_digest=None,
                expected_layer_revision=source_record.layer_revision,
                expected_layer_digest=source_record.layer_digest,
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
            vrn=None,
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
        vrn=None,
        presence="present",
        payload=payload,
        layer_digest=file_snapshot.digest,
        expected_layer_revision=(
            source_record.layer_revision if source_record is not None else None
        ),
        expected_layer_digest=(
            source_record.layer_digest if source_record is not None else None
        ),
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
        vrn=source_record.vrn,
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
