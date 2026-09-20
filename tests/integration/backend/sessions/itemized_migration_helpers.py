"""v1 staging artifact 到 v2 canonical rollout 的迁移契约。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.migration.store import (
    LegacyMigrationStorage,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"


def prepare_migration_workspace(request: pytest.FixtureRequest) -> Path:
    from app.core.path_utils import _cached_session_catalog_components

    # 每个 case 重建同一正式 workspace；旧实例不能跨 case 保留目录索引。
    _cached_session_catalog_components.cache_clear()
    context = TestRunContext.from_test_file(Path(request.node.path))
    return prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )


def _legacy_message(message_id: str, content: str) -> dict[str, object]:
    return {
        "type": "human",
        "data": {
            "content": content,
            "additional_kwargs": {},
            "response_metadata": {},
            "type": "human",
            "name": None,
            "id": message_id,
        },
    }


def _write_v1_source(
    sessions_root: Path,
    *,
    session_id: str,
    session_bundle_factory,
    records: list[dict[str, object]],
) -> bytes:
    session_bundle_factory(sessions_root, session_id)
    # session_bundle_factory 的物理目录名由 resolver 管理；测试通过实际解析
    # 结果取得路径，避免把显示名/目录名关系写死在迁移契约里。
    from app.core.path_utils import get_session_path_resolver

    rollout_root = (
        get_session_path_resolver(sessions_root).resolve_session_node(session_id)
        / "rollout"
    )
    rollout_root.mkdir(parents=True, exist_ok=True)
    source_lines = [
        (
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode()
        for record in records
    ]
    lines = b"".join(source_lines)
    (rollout_root / "rollout.jsonl").write_bytes(lines)
    with sqlite3.connect(rollout_root / "index.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE database_meta (
                singleton_id INTEGER PRIMARY KEY,
                rollout_format_version INTEGER NOT NULL,
                committed_jsonl_offset INTEGER NOT NULL
            );
            CREATE TABLE messages (
                message_sequence INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL,
                turn_id TEXT,
                jsonl_offset INTEGER NOT NULL,
                jsonl_length INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO database_meta(singleton_id, rollout_format_version, committed_jsonl_offset) VALUES (1, 1, ?)",
            (len(lines),),
        )
        offset = 0
        for record, line in zip(records, source_lines, strict=True):
            connection.execute(
                "INSERT INTO messages(message_sequence, message_id, turn_id, jsonl_offset, jsonl_length) VALUES (?, ?, ?, ?, ?)",
                (
                    int(record["message_sequence"]),
                    str(record["message_id"]),
                    record.get("turn_id"),
                    offset,
                    len(line),
                ),
            )
            offset += len(line)
        connection.commit()
    return lines


def _storage(sessions_root: Path) -> LegacyMigrationStorage:
    return LegacyMigrationStorage(
        sessions_root,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )


def _accepted_records() -> list[dict[str, object]]:
    return [
        {
            "format_version": 1,
            "record_type": "message",
            "message_sequence": 1,
            "message_id": "legacy-u1",
            "turn_id": "legacy-turn-1",
            "role": "user",
            "message": _legacy_message("legacy-u1", "迁移前的请求"),
            "metadata": {},
        },
        {
            "format_version": 1,
            "record_type": "message",
            "message_sequence": 2,
            "message_id": "legacy-a1",
            "turn_id": "legacy-turn-1",
            "role": "assistant",
            "message": {
                "type": "ai",
                "data": {
                    "content": "迁移后的回答",
                    "additional_kwargs": {},
                    "response_metadata": {},
                    "type": "ai",
                    "name": None,
                    "id": "legacy-a1",
                },
            },
            "metadata": {"final": True},
        },
    ]


def _table_counts(storage: LegacyMigrationStorage, session_id: str) -> dict[str, int]:
    with storage._connect(session_id, "", read_only=True) as connection:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "item_catalog",
                "messages",
                "turn_records",
                "executions",
                "legacy_migration_reports",
            )
        }


def migration_audits(storage: LegacyMigrationStorage) -> list[dict[str, object]]:
    root = storage.root(TARGET_SESSION_ID).parent / "legacy-import"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/report.json"))]
