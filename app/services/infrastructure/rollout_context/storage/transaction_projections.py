"""checkpoint channel 与 control event 的派生写入 owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from uuid import uuid4

from app.core.hashing import sha256_hex as _hash_bytes
from app.services.infrastructure.rollout_context.storage.checkpoint_state_values import (
    persist_state_value,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutTransactionProjectionMixin:
    """只写 checkpoint/control 的 SQLite projection，不写 canonical item。"""

    def _insert_checkpoint_channels(
        self,
        connection: sqlite3.Connection,
        checkpoint_id: str,
        checkpoint: Mapping[str, object],
        view_id: str,
        timestamp: str,
        *, session_id: str, thread_id: str, checkpoint_ns: str,
        parent_checkpoint_id: str | None, new_versions: Mapping[str, object],
        messages_state: str = "view",
    ) -> None:
        checkpoint_id = strict_text(
            checkpoint_id, field="checkpoint_channels.checkpoint_id"
        )
        view_id = strict_text(view_id, field="checkpoint_channels.context_view_id")
        strict_text(timestamp, field="checkpoint_channels.created_at")
        if messages_state not in {"view", "delta", "snapshot"}:
            raise ValueError(f"checkpoint messages value_state 非法: {messages_state!r}")
        values = checkpoint.get("channel_values", {})
        versions = checkpoint.get("channel_versions", {})
        updated = checkpoint.get("updated_channels") or []
        if (
            not isinstance(values, Mapping)
            or not isinstance(versions, Mapping)
            or not isinstance(updated, list)
        ):
            raise TypeError(
                "checkpoint channel_values/channel_versions/updated_channels 结构非法"
            )
        if any(not isinstance(name, str) or not name for name in values):
            raise TypeError("checkpoint channel_values 的名称必须是非空字符串")
        if any(not isinstance(name, str) or not name for name in versions):
            raise TypeError("checkpoint channel_versions 的名称必须是非空字符串")
        if any(not isinstance(name, str) or not name for name in updated):
            raise TypeError("checkpoint updated_channels 的名称必须是非空字符串")
        updated_index_by_name: dict[str, int] = {}
        for channel_index, name in enumerate(updated):
            if name in updated_index_by_name:
                raise ValueError("checkpoint updated_channels 不得重复")
            updated_index_by_name[name] = channel_index
        # 官方 Delta 根步可以既无值也无版本，仍登记其冻结空 selection。
        names = set(values)
        names.update(versions)
        names.update(updated_index_by_name)
        names.add("messages")
        for name in sorted(names):
            version = versions.get(name)
            if name in values and version is None:
                raise ValueError(f"checkpoint channel 缺少版本: {name}")
            if version is not None:
                version = strict_text(
                    version,
                    field=f"checkpoint channel version: {name}",
                )
            updated_index = updated_index_by_name.get(name)
            if name == "messages":
                result = connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, context_view_id, updated_index, created_at) VALUES (?, ?, 'rollout_view', ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        messages_state,
                        version,
                        view_id,
                        updated_index,
                        timestamp,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"checkpoint channels.messages 写入失败: {checkpoint_id}"
                    )
            elif name in values:
                value_id, value_session_id, value_thread_id = persist_state_value(
                    self, connection, session_id=session_id, thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns, channel=name, version=version,
                    candidate=values[name], parent_checkpoint_id=parent_checkpoint_id,
                    is_new_version=name in new_versions,
                )
                result = connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, value_id, value_session_id, value_thread_id, updated_index, created_at) VALUES (?, ?, 'sqlite_value', 'present', ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        version,
                        value_id,
                        value_session_id,
                        value_thread_id,
                        updated_index,
                        timestamp,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"checkpoint channel 写入失败: {checkpoint_id}/{name}"
                    )
            else:
                result = connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, updated_index, created_at) VALUES (?, ?, 'sqlite_value', 'absent', ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        version,
                        updated_index,
                        timestamp,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"checkpoint absent channel 写入失败: {checkpoint_id}/{name}"
                    )

    def _insert_control(
        self,
        connection: sqlite3.Connection,
        kind: str,
        entity_type: str,
        entity_id: str,
        branch_id: str | None,
        view_id: str | None,
        checkpoint_id: str | None,
        payload: Mapping[str, object],
        transaction_id: str,
        timestamp: str,
    ) -> int:
        strict_text(timestamp, field="control_events.created_at")
        strict_text(kind, field="control_events.control_kind")
        strict_text(entity_type, field="control_events.entity_type")
        strict_text(entity_id, field="control_events.entity_id")
        strict_optional_text(branch_id, field="control_events.branch_id")
        strict_optional_text(view_id, field="control_events.view_id")
        strict_optional_text(checkpoint_id, field="control_events.checkpoint_id")
        strict_text(transaction_id, field="control_events.transaction_id")
        if not isinstance(payload, Mapping):
            raise TypeError("control event payload 必须是 object")
        previous = connection.execute(
            "SELECT event_hash FROM control_events ORDER BY control_sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = (
            strict_text(previous[0], field="control_events.event_hash")
            if previous
            else ""
        )
        event_hash = _hash_bytes(
            _json(
                {
                    "kind": kind,
                    "entity": entity_id,
                    "payload": payload,
                    "previous": previous_hash,
                }
            ).encode()
        )
        cursor = connection.execute(
            "INSERT INTO control_events(control_id, control_kind, entity_type, entity_id, branch_id, view_id, checkpoint_id, payload_json, transaction_id, previous_event_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                kind,
                entity_type,
                entity_id,
                branch_id,
                view_id,
                checkpoint_id,
                _json(payload),
                transaction_id,
                previous_hash or None,
                event_hash,
                timestamp,
            ),
        )
        return strict_non_negative_int(
            cursor.lastrowid,
            field="control_events.control_sequence",
        )


__all__ = ["RolloutTransactionProjectionMixin"]
