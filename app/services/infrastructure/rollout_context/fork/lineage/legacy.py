"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from app.services.infrastructure.rollout_context.fork.validation import (
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_non_negative_int,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _legacy_source_identity_map(
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
) -> dict[str, dict[str, str]]:
    """从 v1 staging report 读取 source 坐标到 target identity 的映射。

    v1 没有 v2 的 turn/execution/item 主键；迁移 report 中的 synthetic
    identity 和每个 legacy message 坐标才是可审计的 source lineage。这里
    不扫描 source rollout，也不把 target-local ID 重新当成 source ID。
    """
    row = connection.execute(
        "SELECT report_json FROM legacy_migration_reports WHERE source_session_id = ? AND target_session_id = ? AND source_format_version = 1 AND status IN ('completed', 'completed_with_rejections') ORDER BY created_at DESC LIMIT 1",
        (source_session_id, target_session_id),
    ).fetchone()
    if row is None:
        return {}
    report_text = strict_text(row[0], field="legacy_migration_reports.report_json")
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("legacy migration report JSON 无法解析") from error
    if not isinstance(report, Mapping):
        raise TypeError("legacy migration report 必须是 object")
    migrated = report.get("migrated")
    if not isinstance(migrated, list):
        raise TypeError("legacy migration report migrated 必须是 list")
    result: dict[str, dict[str, str]] = {
        "turn": {},
        "execution": {},
        "item": {},
        "accepted_ingress": {},
        "acceptance_idempotency_key": {},
    }
    for entry in migrated:
        if not isinstance(entry, Mapping):
            raise TypeError("legacy migration report migrated entry 必须是 object")
        target_turn = entry.get("target_turn_id")
        source_turn = entry.get("source_turn_id")
        target_turn = required_text(target_turn, field="migration.target_turn_id")
        source_turn = optional_text(source_turn, field="migration.source_turn_id")
        if source_turn is not None:
            result["turn"][target_turn] = f"legacy:v1:turn:{source_turn}"
        target_execution = entry.get("target_initial_execution_id")
        source_execution = entry.get("source_initial_execution_id")
        target_execution = required_text(
            target_execution, field="migration.target_initial_execution_id"
        )
        source_execution = optional_text(
            source_execution, field="migration.source_initial_execution_id"
        )
        if source_execution is not None:
            result["execution"][target_execution] = (
                f"legacy:v1:execution:{source_execution}"
            )
        target_ingress = entry.get("target_accepted_ingress_id")
        source_ingress = entry.get("source_accepted_ingress_id")
        target_ingress = required_text(
            target_ingress, field="migration.target_accepted_ingress_id"
        )
        source_ingress = optional_text(
            source_ingress, field="migration.source_accepted_ingress_id"
        )
        if source_ingress is not None:
            result["accepted_ingress"][target_ingress] = (
                f"legacy:v1:accepted-ingress:{source_ingress}"
            )
        target_key = entry.get("target_acceptance_idempotency_key")
        source_key = entry.get("source_acceptance_idempotency_key")
        target_key = required_text(
            target_key, field="migration.target_acceptance_idempotency_key"
        )
        source_key = optional_text(
            source_key, field="migration.source_acceptance_idempotency_key"
        )
        if source_key is not None:
            result["acceptance_idempotency_key"][target_key] = (
                f"legacy:v1:acceptance-key:{source_key}"
            )
    for target_item, metadata_json in connection.execute(
        "SELECT item_id, metadata_json FROM item_catalog ORDER BY item_sequence"
    ).fetchall():
        target_item = required_text(target_item, field="item_catalog.item_id")
        metadata_text = required_text(
            metadata_json, field=f"item_catalog.metadata_json:{target_item}"
        )
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"legacy migrated item metadata JSON 无法解析: {target_item}"
            ) from error
        if not isinstance(metadata, Mapping):
            raise TypeError(
                f"legacy migrated item metadata 必须是 object: {target_item}"
            )
        source_ref = metadata.get("legacy_source_ref")
        if not isinstance(source_ref, Mapping):
            continue
        source_message_id = source_ref.get("message_id")
        source_message_id = optional_text(
            source_message_id, field=f"legacy_source_ref.message_id:{target_item}"
        )
        if source_message_id is not None:
            part_id = optional_text(
                source_ref.get("part_id"), field="legacy_source_ref.part_id"
            )
            result["item"][target_item] = (
                "legacy:v1:message-part:"
                + _json({"message_id": source_message_id, "part_id": part_id})
                if part_id is not None
                else f"legacy:v1:message:{source_message_id}"
            )
    return result


def _legacy_source_item_offsets(
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
) -> dict[str, int]:
    """返回迁移后 target item 对应的 v1 source JSONL byte offset。

    ``item_catalog.jsonl_offset`` 只能描述 target-local v2 artifact。v1
    full-copy 的 source 坐标来自 staging report；如果继续把 target offset
    写入 source_offset，审计记录会看起来完整但实际上失去 source provenance。
    """
    row = connection.execute(
        "SELECT report_json FROM legacy_migration_reports "
        "WHERE source_session_id = ? AND target_session_id = ? "
        "AND source_format_version = 1 "
        "AND status IN ('completed', 'completed_with_rejections') "
        "ORDER BY created_at DESC LIMIT 1",
        (source_session_id, target_session_id),
    ).fetchone()
    if row is None:
        return {}
    report_text = strict_text(row[0], field="legacy_migration_reports.report_json")
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("legacy migration report JSON 无法解析") from error
    if not isinstance(report, Mapping):
        raise TypeError("legacy migration report 必须是 object")
    source_offsets: dict[str, int] = {}
    migrated = report.get("migrated")
    if isinstance(migrated, list):
        for entry in migrated:
            if not isinstance(entry, Mapping):
                raise TypeError("legacy migration report migrated entry 必须是 object")
            offsets = entry.get("source_item_offsets")
            if not isinstance(offsets, Mapping):
                raise TypeError("legacy migration source_item_offsets 必须是 object")
            for source_item_id, raw_offset in offsets.items():
                source_item_id = required_text(
                    source_item_id, field="migration.source_item_offsets.item_id"
                )
                raw_offset = strict_optional_non_negative_int(
                    raw_offset,
                    field=f"migration.source_item_offsets:{source_item_id}",
                )
                if raw_offset is not None:
                    source_offsets[source_item_id] = raw_offset

    target_offsets: dict[str, int] = {}
    for target_item_id, metadata_json in connection.execute(
        "SELECT item_id, metadata_json FROM item_catalog ORDER BY item_sequence"
    ).fetchall():
        target_item_id = required_text(target_item_id, field="item_catalog.item_id")
        metadata_text = required_text(
            metadata_json, field=f"item_catalog.metadata_json:{target_item_id}"
        )
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"legacy migrated item metadata JSON 无法解析: {target_item_id}"
            ) from error
        if not isinstance(metadata, Mapping):
            raise TypeError(
                f"legacy migrated item metadata 必须是 object: {target_item_id}"
            )
        source_item_id = metadata.get("legacy_source_item_id")
        if not isinstance(source_item_id, str) or not source_item_id:
            continue
        source_offset = source_offsets.get(source_item_id)
        if source_offset is not None:
            target_offsets[target_item_id] = source_offset
    return target_offsets
