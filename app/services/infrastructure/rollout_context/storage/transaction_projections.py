"""checkpoint channel 与 control event 的派生写入 owner。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class RolloutTransactionProjectionMixin:
    """只写 checkpoint/control 的 SQLite projection，不写 canonical item。"""

    def _insert_checkpoint_channels(
        self,
        connection: sqlite3.Connection,
        checkpoint_id: str,
        checkpoint: Mapping[str, object],
        view_id: str,
        timestamp: str,
    ) -> None:
        checkpoint_id = strict_text(
            checkpoint_id, field="checkpoint_channels.checkpoint_id"
        )
        view_id = strict_text(view_id, field="checkpoint_channels.context_view_id")
        strict_text(timestamp, field="checkpoint_channels.created_at")
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
        if len(set(updated)) != len(updated):
            raise ValueError("checkpoint updated_channels 不得重复")
        names = set(values) | set(versions) | set(updated)
        for name in sorted(names):
            version = versions.get(name)
            if name in values and version is None:
                raise ValueError(f"checkpoint channel 缺少版本: {name}")
            if version is not None:
                version = strict_text(
                    version,
                    field=f"checkpoint channel version: {name}",
                )
            updated_index = updated.index(name) if name in updated else None
            if name == "messages":
                result = connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, context_view_id, updated_index, created_at) VALUES (?, ?, 'rollout_view', 'view', ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
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
                serializer, blob, length, digest = self._encode(values[name])
                result = connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, serializer_name, value_blob, value_length, value_hash, updated_index, created_at) VALUES (?, ?, 'sqlite_value', 'present', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        version,
                        serializer,
                        blob,
                        length,
                        digest,
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
