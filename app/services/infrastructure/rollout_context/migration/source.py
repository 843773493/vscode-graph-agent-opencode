"""仅供一次性导入使用的 v1 严格只读 manifest/envelope reader。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
    read_regular,
)
from app.services.infrastructure.rollout_context.migration.legacy_adapter import (
    LegacyRolloutAdapter,
    validate_envelope,
)
from app.services.infrastructure.rollout_context.migration.manifest import (
    apply_final_pointers,
    final_pointers,
    message_checks,
    validate_message_checks,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FormatDispatchError(f"v1 JSON object 重复字段: {key}")
        result[key] = value
    return result


def read_source_report(source_root: Path, session_id: str) -> dict[str, object]:
    files = artifact_manifest(source_root)
    for name in ("index.sqlite", "rollout.jsonl"):
        if name not in files:
            raise FileNotFoundError(f"v1 migration source artifact 不完整: {name}")
    # immutable SQLite 不创建 WAL/SHM，也不会执行源库 journal recovery。
    # 非空事务文件必须先由旧版本的离线导出流程收敛，不能忽略后谎报完整。
    for name in ("index.sqlite-wal", "index.sqlite-journal"):
        if name in files and files[name]["size"]:
            raise FormatDispatchError(f"v1_source_requires_offline_snapshot: {name}")
    index = source_root / "index.sqlite"
    raw_jsonl = read_regular(source_root / "rollout.jsonl")
    with closing(
        sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        meta_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        required_meta = {
            "singleton_id",
            "rollout_format_version",
            "committed_jsonl_offset",
        }
        if not required_meta.issubset(meta_columns):
            raise FormatDispatchError(
                "v1 database_meta 缺少字段: "
                + ",".join(sorted(required_meta - meta_columns))
            )
        meta_rows = connection.execute(
            "SELECT singleton_id, rollout_format_version, committed_jsonl_offset FROM database_meta"
        ).fetchall()
        if len(meta_rows) != 1 or meta_rows[0][0] != 1:
            raise FormatDispatchError("v1 database_meta 必须恰有一个 singleton row")
        version = strict_non_negative_int(
            meta_rows[0][1], field="v1 rollout_format_version"
        )
        if version != 1:
            raise FormatDispatchError(
                f"legacy adapter 只接受 rollout_format_version=1，实际为 {version}"
            )
        committed = strict_non_negative_int(
            meta_rows[0][2], field="v1 database_meta.committed_jsonl_offset"
        )
        if committed > len(raw_jsonl):
            raise FormatDispatchError(
                "v1 rollout.jsonl 与 source committed offset 不一致"
            )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        required = {
            "message_sequence",
            "message_id",
            "turn_id",
            "jsonl_offset",
            "jsonl_length",
        }
        if not required.issubset(columns):
            raise FormatDispatchError(
                "v1 messages manifest 缺少字段: " + ",".join(sorted(required - columns))
            )
        rows = connection.execute(
            "SELECT message_sequence, message_id, turn_id, jsonl_offset, jsonl_length FROM messages ORDER BY message_sequence"
        ).fetchall()
        checks = message_checks(connection, columns)
        final_refs = final_pointers(connection)
        extra_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] not in {"database_meta", "messages"}
        ]
        preserved_tables = {}
        for table in extra_tables:
            quoted = '"' + table.replace('"', '""') + '"'
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            if count:
                preserved_tables[table] = count
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise FormatDispatchError("v1 SQLite integrity_check 失败")
    records: list[dict[str, object]] = []
    next_offset = 0
    previous_sequence = 0
    for (
        sequence_value,
        message_id_value,
        turn_value,
        offset_value,
        length_value,
    ) in rows:
        sequence = strict_non_negative_int(
            sequence_value, field="v1 messages.message_sequence"
        )
        message_id = strict_text(message_id_value, field="v1 messages.message_id")
        turn_id = strict_optional_text(turn_value, field="v1 messages.turn_id")
        offset = strict_non_negative_int(offset_value, field="v1 messages.jsonl_offset")
        length = strict_non_negative_int(length_value, field="v1 messages.jsonl_length")
        if sequence <= previous_sequence or offset != next_offset or length == 0:
            raise FormatDispatchError(
                f"v1 manifest sequence/offset/length 不连续或重叠: {message_id}"
            )
        if offset + length > committed:
            raise FormatDispatchError(
                f"legacy message 超出 committed offset: {message_id}"
            )
        raw_line = raw_jsonl[offset : offset + length]
        if not raw_line.endswith(b"\n") or b"\n" in raw_line[:-1]:
            raise FormatDispatchError(
                f"v1 manifest 必须指向一条完整 JSONL 行: {message_id}"
            )
        envelope = json.loads(
            raw_line.decode("utf-8"), object_pairs_hook=_unique_object
        )
        if not isinstance(envelope, Mapping):
            raise FormatDispatchError(f"legacy envelope 不是 object: {message_id}")
        validate_envelope(envelope, expected_format=1)
        if "legacy_manifest_final" in envelope:
            raise FormatDispatchError(
                "v1 envelope 不得伪造 migration manifest final evidence"
            )
        validate_message_checks(envelope, checks.get(sequence, {}))
        if (
            envelope["message_sequence"],
            envelope["message_id"],
            envelope["turn_id"],
        ) != (sequence, message_id, turn_id):
            raise FormatDispatchError(
                f"legacy message manifest 与 JSONL envelope 不一致: {message_id}"
            )
        payload_hash = sha256_jcs(envelope["message"])
        if "payload_hash" in envelope and envelope["payload_hash"] != payload_hash:
            raise FormatDispatchError(f"source-mismatch: v1 payload_hash: {message_id}")
        records.append(
            {
                **envelope,
                "payload_hash": payload_hash,
                "jsonl_offset": offset,
                "jsonl_length": length,
                "raw_line_sha256": hashlib.sha256(raw_line).hexdigest(),
            }
        )
        previous_sequence, next_offset = sequence, offset + length
    if next_offset != committed:
        raise FormatDispatchError("v1 manifest 未覆盖全部 committed JSONL 字节")
    apply_final_pointers(records, final_refs)
    if artifact_manifest(source_root) != files:
        raise FormatDispatchError("source-mismatch: v1 report 读取期间原件变化")
    return {
        "source_session_id": session_id,
        "source_format_version": 1,
        "target_format_version": 2,
        "read_only": True,
        "dispatch_contract": {
            "source_reader": "legacy_import_v1_to_v2_only",
            "target_runtime_format_version": 2,
            "v1_runtime_fallback": False,
            "source_coordinates_are_audit_only": True,
        },
        "source_artifacts": {
            "rollout_jsonl": {
                **files["rollout.jsonl"],
                "path": str(source_root / "rollout.jsonl"),
                "committed_offset": committed,
            },
            "index_sqlite": {**files["index.sqlite"], "path": str(index)},
        },
        "source_files": files,
        "loss": [
            *(
                [
                    {
                        "reason": "legacy_sqlite_state_preserved_raw",
                        "tables": preserved_tables,
                    }
                ]
                if preserved_tables
                else []
            ),
            *(
                [
                    {
                        "reason": "uncommitted_tail_quarantined",
                        "offset": committed,
                        "length": len(raw_jsonl) - committed,
                    }
                ]
                if committed < len(raw_jsonl)
                else []
            ),
            *(
                [
                    {
                        "reason": "legacy_auxiliary_artifacts_preserved_raw",
                        "files": sorted(set(files) - {"rollout.jsonl", "index.sqlite"}),
                    }
                ]
                if set(files) - {"rollout.jsonl", "index.sqlite"}
                else []
            ),
        ],
        "candidates": LegacyRolloutAdapter(session_id).group_candidates(records),
    }
