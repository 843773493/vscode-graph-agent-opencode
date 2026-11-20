"""v1 manifest 中已知的正文校验和与显式 final pointer。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)


def message_checks(
    connection: sqlite3.Connection, columns: set[str]
) -> dict[int, dict[str, object]]:
    selected = sorted(columns & {"role", "content_hash", "content_length"})
    if not selected:
        return {}
    rows = connection.execute(
        "SELECT message_sequence," + ",".join(selected) + " FROM messages"
    ).fetchall()
    return {row[0]: dict(zip(selected, row[1:], strict=True)) for row in rows}


def validate_message_checks(
    record: Mapping[str, object], checks: Mapping[str, object]
) -> None:
    message = record["message"]
    data = message.get("data")
    if "role" in checks and checks["role"] != record["role"]:
        raise FormatDispatchError("v1 manifest role 与 envelope 不一致")
    if not isinstance(data, Mapping) or "content" not in data:
        if "content_hash" in checks or "content_length" in checks:
            raise FormatDispatchError("v1 content checksum 缺少 message.data.content")
        return
    # v1 正文校验采用旧 writer 的精确 JSON 编码；这不是 v2 JCS hash。
    body = json.dumps(
        data["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        "content_hash" in checks
        and checks["content_hash"] != hashlib.sha256(body).hexdigest()
    ):
        raise FormatDispatchError("source-mismatch: v1 manifest content_hash")
    if "content_length" in checks and strict_non_negative_int(
        checks["content_length"], field="v1 content_length"
    ) != len(body):
        raise FormatDispatchError("source-mismatch: v1 manifest content_length")


def final_pointers(connection: sqlite3.Connection) -> list[dict[str, object]]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
    fields = ["turn_id", "status", "final_message_id", "final_message_sequence"]
    if not set(fields).issubset(columns):
        return []
    return [
        dict(zip(fields, row, strict=True))
        for row in connection.execute("SELECT " + ",".join(fields) + " FROM turns")
    ]


def apply_final_pointers(
    records: list[dict[str, object]], pointers: list[dict[str, object]]
) -> None:
    by_sequence = {record["message_sequence"]: record for record in records}
    for pointer in pointers:
        sequence = pointer["final_message_sequence"]
        message_id = pointer["final_message_id"]
        if sequence is None and message_id is None:
            continue
        sequence = strict_non_negative_int(sequence, field="v1 final_message_sequence")
        record = by_sequence.get(sequence)
        if (
            record is None
            or record["message_id"] != message_id
            or record["turn_id"] != pointer["turn_id"]
            or record["role"] != "assistant"
            or pointer["status"] != "completed"
        ):
            raise FormatDispatchError(
                "v1 manifest final pointer 不是同一 Turn 的 completed assistant"
            )
        record["legacy_manifest_final"] = True
