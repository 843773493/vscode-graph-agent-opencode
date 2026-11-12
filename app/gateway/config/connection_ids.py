from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config.state import ConfigConflictError, new_config_id

if TYPE_CHECKING:
    from app.gateway.control.gateway_state import GatewayConfigRecord

_CONNECTION_ID_RECORD_VERSION = 4


def _connection_id_record(
    state_store: GatewayStateStore,
) -> GatewayConfigRecord | None:
    record = state_store.get_config("gateway_connection_ids")
    if record is None:
        return None
    if record.config_version != _CONNECTION_ID_RECORD_VERSION:
        raise ConfigConflictError(
            "Gateway connection_id 旧维护记录需要显式运行 "
            "`python -m configs.boxteam migrate-runtime-config`"
        )
    if record.payload.get("migration") is not None:
        raise ConfigConflictError(
            "Gateway connection_id 存在旧维护记录；请显式运行 "
            "`python -m configs.boxteam migrate-runtime-config`"
        )
    return record


def _connection_identity_fingerprint(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item.get("host"),
        item.get("port", 22),
        item.get("username"),
        item.get("private_key_path"),
        item.get("ssh_config_host"),
        item.get("remote_gateway_port", 8014),
    )


def _normalize_connection_ids(
    raw_config: dict[str, object],
    *,
    state_store: GatewayStateStore | None,
    previous_entries: list[dict[str, object]] | None = None,
    persist_migrations: bool = True,
    allow_new_ids: bool = True,
    explicit_migration: bool = False,
) -> list[dict[str, object]]:
    raw_workspaces = raw_config.get("workspaces")
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    items: list[dict[str, object]] = []
    for item in raw_workspaces:
        if not isinstance(item, dict):
            raise TypeError("Gateway workspaces 元素必须是对象")
        items.append(item)

    source_version = raw_config.get("config_version", 1)
    if not isinstance(source_version, int):
        raise TypeError("Gateway config_version 必须是整数")
    if (
        source_version == 1
        and any(item.get("connection_id") is None for item in items)
        and not explicit_migration
    ):
        raise ValueError(
            "Gateway v1 workspaces 缺少 connection_id；普通配置加载不会自动迁移。"
            "请显式运行 `python -m configs.boxteam migrate-runtime-config`"
        )

    previous_entries = list(previous_entries or ())
    identity_record = _connection_id_record(state_store) if state_store is not None else None
    if identity_record is not None:
        raw_entries = identity_record.payload.get("entries", [])
        if not isinstance(raw_entries, list) or not all(
            isinstance(entry, dict) for entry in raw_entries
        ):
            raise ValueError("Gateway connection_id 记录损坏")
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
                raise ValueError("Gateway connection_id 记录存在重复匹配")
            if matching:
                connection_id = cast(str, matching[0]["connection_id"])
                unmatched_previous.remove(matching[0])
            elif allow_new_ids and position < len(previous_entries):
                position_entry = previous_entries[position]
                connection_id = cast(str, position_entry["connection_id"])
                if position_entry in unmatched_previous:
                    unmatched_previous.remove(position_entry)
            elif allow_new_ids:
                connection_id = new_config_id("connection")
            else:
                raise ConfigConflictError(
                    f"Gateway 来源 workspaces[{position}].connection_id 缺失；"
                    "新增连接必须显式填写稳定 connection_id"
                )
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

    if state_store is not None and persist_migrations:
        normalized_payload: dict[str, object] = {"entries": normalized_entries}
        if identity_record is None or identity_record.payload != normalized_payload:
            state_store.set_config(
                config_key="gateway_connection_ids",
                config_version=_CONNECTION_ID_RECORD_VERSION,
                payload=normalized_payload,
            )
    return normalized_entries
