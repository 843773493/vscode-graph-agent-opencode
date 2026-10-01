from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from itertools import pairwise
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_migration import migrate_workspace_session_catalog
from app.domain.itemized.errors import FormatDispatchError
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.store import (
    LegacyMigrationStorage,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.integration.backend.sessions.deterministic_rollout_fixture_helpers import (
    seed_deterministic_v2_rollout,
)
from tests.support.paths import output_root_for_test
from tests.support.workspaces import prepare_default_test_workspace

REAL_SESSION_ID = "ses_8128d7f0a4b64aa0b3f1c9e7d2a65018"
STATIC_MOCK_SESSION_ID = "ses_a1b2c3d4e5f6478899aabbccddeeff00"


@pytest.fixture
def assert_fixture_import_rejected(
    real_rollout_workspace: Path, session_bundle_factory
) -> Callable[[str], None]:
    """无 dispatch 的旧快照只能显式拒绝；失败保留完整原件与审计副本。"""
    sessions = real_rollout_workspace / ".boxteam" / "sessions"
    storage = LegacyMigrationStorage(sessions)

    def import_source(source_id: str) -> None:
        target_id = f"ses_{uuid4().hex}"
        source = storage.root(source_id)
        template = (
            Path.cwd()
            / "tests/fixtures/workspaces/custom_tool_test_workspace/.boxteam/sessions"
            / source_id
            / "rollout"
        )
        source_before, template_before = (
            artifact_manifest(source),
            artifact_manifest(template),
        )
        session_bundle_factory(sessions, target_id)
        with pytest.raises(FormatDispatchError, match="rollout_format_version"):
            storage.migrate_legacy_to_v2(
                source_id, target_thread_id=target_id, require_lossless=True
            )
        assert artifact_manifest(source) == source_before
        assert artifact_manifest(template) == template_before
        assert not storage.root(target_id).exists()
        audit_root = storage.root(target_id).parent / "legacy-import"
        audits = list(audit_root.glob("*/report.json"))
        assert len(audits) == 1
        audit = json.loads(audits[0].read_text())
        assert audit["status"] == "failed"
        assert audit["rollback"] == "uninstalled_staging_quarantined"
        assert artifact_manifest(audits[0].parent / "source") == source_before
        # 未发布目标不能借 initialize 当作迁移成功，原件也不能交给 v2 runtime。
        with (
            closing(
                sqlite3.connect(
                    (source / "index.sqlite").as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
            ) as connection,
            pytest.raises(ValueError, match="v1_migration_required"),
        ):
            RolloutStorage(sessions)._require_v2_runtime(connection)
        assert artifact_manifest(source) == source_before

    return import_source


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    """用 custom_tool_test_workspace 的完整副本作为本文件的集成测试工作区。"""

    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root
        / "tests"
        / "fixtures"
        / "workspaces"
        / "custom_tool_test_workspace",
        shared_skill_root=project_root / "resources" / "skills",
    )
    # 模板是旧 JSON 权威索引形态；先建立完整 SQLite catalog authority，
    # 否则唯一 resolver 会按契约 fail-closed 拒绝旧 JSON。
    asyncio.run(migrate_workspace_session_catalog(workspace_root=workspace_root))
    return str(workspace_root)


@pytest.fixture
def real_rollout_workspace(
    integration_workspace_root_path: str,
) -> Path:
    return Path(integration_workspace_root_path)


def test_custom_tool_fixture_asset_contract(
    real_rollout_workspace: Path,
    assert_fixture_import_rejected: Callable[[str], None],
) -> None:
    manifest_path = real_rollout_workspace / "rollout-fixture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sessions = manifest["sessions"]
    session_ids = {item["session_id"] for item in sessions}
    assert REAL_SESSION_ID in session_ids
    assert STATIC_MOCK_SESSION_ID in session_ids
    assert REAL_SESSION_ID != STATIC_MOCK_SESSION_ID

    sessions_root = real_rollout_workspace / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    for item in sessions:
        session_id = item["session_id"]
        session_node = resolver.resolve_session_node(session_id)
        session_manifest = json.loads(
            (session_node / "session.json").read_text(encoding="utf-8")
        )
        assert session_manifest["workspace_id"] == "ws_local"
        rollout_root = session_node / "rollout"
        assert {child.name for child in rollout_root.iterdir()} == {
            "index.sqlite",
            "rollout.jsonl",
        }
        assert not list(rollout_root.glob("segment-*.jsonl"))
        assert not (session_node / "payloads").exists()

    compact_index = (
        resolver.resolve_session_node("ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345")
        / "rollout"
        / "index.sqlite"
    )
    with closing(
        sqlite3.connect(compact_index.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        checkpoint_id = connection.execute(
            "SELECT checkpoint_id FROM checkpoints ORDER BY commit_id DESC LIMIT 1"
        ).fetchone()[0]
        event_version = connection.execute(
            "SELECT channel_version FROM checkpoint_channels "
            "WHERE checkpoint_id = ? AND channel_name = '_summarization_event'",
            (checkpoint_id,),
        ).fetchone()[0]
        messages_version = connection.execute(
            "SELECT channel_version FROM checkpoint_channels "
            "WHERE checkpoint_id = ? AND channel_name = 'messages'",
            (checkpoint_id,),
        ).fetchone()[0]
        assert str(event_version).split(".", 1)[0].isdigit()
        assert str(messages_version).split(".", 1)[0].isdigit()

        serializer, blob, length, digest = connection.execute(
            "SELECT serializer_name, value_blob, value_length, value_hash "
            "FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='_summarization_event'",
            (checkpoint_id,),
        ).fetchone()
        import hashlib

        assert len(blob) == length
        assert hashlib.sha256(blob).hexdigest() == digest
        # 仅审核可信 fixture 的原始 BLOB，不通过 runtime 的 v1 checkpoint reader。
        compact_event = JsonPlusSerializer().loads_typed((serializer, blob))
    assert_fixture_import_rejected("ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345")
    assert compact_event["strategy"] == "cache_preserving"
    assert compact_event["cutoff_index"] == 64
    assert compact_event["cache_prefix_messages"] == []
    assert compact_event["summary_message"].additional_kwargs["lc_source"] == (
        "summarization"
    )


def _reader(workspace_root: Path) -> RolloutHistoryReader:
    return RolloutHistoryReader(
        RolloutContextReader(
            RolloutStorage(
                workspace_root / ".boxteam" / "sessions",
                message_codec=LangChainMessageCodec(),
            )
        )
    )


def _advance_to_64_cursor(
    reader: RolloutHistoryReader,
    session_id: str,
) -> str:
    _, cursor, _ = reader.bootstrap(session_id)
    assert cursor is not None
    first = reader.load(
        session_id,
        TurnHistoryLoadRequest(direction="before", cursor=cursor),
    )
    assert [item.ordinal for item in first.items] == [125, 126, 127]
    second = reader.load(
        session_id,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=first.next_cursor,
            turns=16,
        ),
    )
    assert [item.ordinal for item in second.items] == list(range(109, 125))
    assert second.next_cursor is not None
    return second.next_cursor


def test_legacy_128_turn_fixture_is_preserved_and_explicitly_rejected(
    real_rollout_workspace: Path,
    assert_fixture_import_rejected: Callable[[str], None],
) -> None:
    sessions_dir = real_rollout_workspace / ".boxteam" / "sessions"
    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(REAL_SESSION_ID)
        / "rollout"
    )
    jsonl_path = rollout_root / "rollout.jsonl"
    sqlite_path = rollout_root / "index.sqlite"
    assert jsonl_path.is_file()
    assert sqlite_path.is_file()
    assert not list(rollout_root.glob("segment-*.jsonl"))

    with closing(
        sqlite3.connect(sqlite_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 128
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] >= 128
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM turns WHERE status = 'completed' AND final_message_sequence IS NOT NULL"
            ).fetchone()[0]
            == 128
        )
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] >= 16
        assert (
            connection.execute("SELECT COUNT(*) FROM reasoning_blocks").fetchone()[0]
            > 0
        )
        reasoning_carriers = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT carrier_type FROM reasoning_blocks"
            )
        }
        assert {
            "reasoning_content",
            "reasoning_items",
            "redacted_thinking",
        }.issubset(reasoning_carriers)
        checkpoint_providers = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT json_extract(metadata_json, '$.provider_id')
                FROM checkpoints
                WHERE json_extract(metadata_json, '$.provider_id') IS NOT NULL
                """
            )
        }
        assert len(checkpoint_providers) >= 2
        provider_sequence = [
            row[0]
            for row in connection.execute(
                """
                SELECT json_extract(c.metadata_json, '$.provider_id')
                FROM turns AS t
                JOIN checkpoints AS c
                  ON c.checkpoint_id = printf('real-checkpoint-%04d', t.turn_ordinal)
                ORDER BY t.turn_ordinal
                """
            )
        ]
        assert len(provider_sequence) == 128
        assert (
            sum(
                previous != current for previous, current in pairwise(provider_sequence)
            )
            >= 16
        )

    records = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(records) >= 288
    assert {record["role"] for record in records} == {"user", "assistant", "tool"}
    assert all("payload_ref" not in record for record in records)
    message_providers = {
        record["message"]["data"]["response_metadata"]["provider_id"]
        for record in records
        if isinstance(record.get("message"), dict)
        and isinstance(record["message"].get("data"), dict)
        and isinstance(record["message"]["data"].get("response_metadata"), dict)
        and isinstance(
            record["message"]["data"]["response_metadata"].get("provider_id"),
            str,
        )
    }
    assert len(message_providers) >= 2
    assistant_records = [
        record["message"]["data"]
        for record in records
        if record.get("role") == "assistant"
    ]
    assert assistant_records
    assert all(
        "invalid_tool_calls" in message
        and isinstance(message["invalid_tool_calls"], list)
        for message in assistant_records
    )
    assert all(
        not {
            "reasoning_content",
            "thinking_blocks",
            "reasoning_items",
        }.intersection(message.get("additional_kwargs", {}))
        for message in assistant_records
    )
    reasoning_records = [
        message
        for message in assistant_records
        if any(
            isinstance(block, dict)
            and block.get("type") in {"reasoning", "thinking", "redacted_thinking"}
            for block in message.get("content", [])
        )
    ]
    assert len(reasoning_records) >= 64
    assert all(
        all(
            not (isinstance(block, dict) and block.get("type") == "litellm_payload")
            for block in message["content"]
        )
        for message in assistant_records
    )

    assert_fixture_import_rejected(REAL_SESSION_ID)


@pytest.fixture
def deterministic_v2_rollout(real_rollout_workspace: Path, session_bundle_factory):
    """确定性集成数据由真实 Saver 写入，不是旧真实 Provider 记录。"""
    return seed_deterministic_v2_rollout(real_rollout_workspace, session_bundle_factory)


def test_deterministic_v2_128_turn_history_is_bounded_and_preserves_tool_body(
    real_rollout_workspace: Path,
    deterministic_v2_rollout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, tool_bodies = deterministic_v2_rollout
    reader = _reader(real_rollout_workspace)
    storage = reader._context_reader._storage
    items = storage.read_items(session_id)
    assert len([item for item in items if item.semantic_kind == "user_input"]) == 128
    assert all(item.format_version == 2 for item in items)
    stored_results = [item for item in items if item.semantic_kind == "tool_result"]
    assert len(stored_results) == len(tool_bodies) == 16
    for body in tool_bodies.values():
        assert any(
            body in json.dumps(item.payload, ensure_ascii=False)
            for item in stored_results
        )
    with storage._connect(session_id, "", read_only=True) as connection:
        assert connection.execute(
            "SELECT schema_version, rollout_format_version FROM database_meta"
        ).fetchone() == (ROLLOUT_SCHEMA_VERSION, 2)
    source_by_sequence = {item.item_sequence: item for item in items}
    accessed: list[int] = []
    original_read = storage._read_record_envelopes

    def capture_read(thread_id, checkpoint_ns, rows, *, connection=None):
        rows = list(rows)
        accessed.extend(row[0] for row in rows)
        return original_read(thread_id, checkpoint_ns, rows, connection=connection)

    def reject_full_scan(*args, **kwargs):
        raise AssertionError("有界 history 不得 materialize 全量消息")

    monkeypatch.setattr(storage, "_read_record_envelopes", capture_read)
    monkeypatch.setattr(storage, "materialize_messages", reject_full_scan)
    tail = reader.load(session_id, TurnHistoryLoadRequest(direction="tail", turns=1))
    assert [item.ordinal for item in tail.items] == [128]
    assert tail.items[0].final_response == "确定性集成回答 128"
    assert tail.items[0].tool_summary
    assert len(accessed) <= 4
    assert all(
        source_by_sequence[seq].semantic_kind != "tool_result" for seq in accessed
    )
    accessed.clear()
    detail = reader.load(
        session_id,
        TurnHistoryLoadRequest(
            turn_ids=["deterministic-turn-0128"],
            tool_call_ids=["deterministic-call-0128"],
            include=["tool_call", "tool_result"],
        ),
    )
    rendered = detail.model_dump(mode="json")
    assert detail.items[0].detail_truncated is True
    assert tool_bodies["deterministic-call-0128"] not in json.dumps(
        rendered, ensure_ascii=False
    )
    assert tool_bodies["deterministic-call-0120"] not in json.dumps(
        rendered, ensure_ascii=False
    )
    assert len(accessed) <= 3
    assert all(
        source_by_sequence[seq].turn_id == "deterministic-turn-0128" for seq in accessed
    )
    accessed.clear()
    small_detail = reader.load(
        session_id,
        TurnHistoryLoadRequest(
            turn_ids=["deterministic-turn-0112"],
            tool_call_ids=["deterministic-call-0112"],
            include=["tool_call", "tool_result"],
        ),
    )
    assert small_detail.items[0].detail_truncated is False
    assert tool_bodies["deterministic-call-0112"] in json.dumps(
        small_detail.model_dump(mode="json"), ensure_ascii=False
    )
    assert len(accessed) <= 3
    assert all(
        source_by_sequence[seq].turn_id == "deterministic-turn-0112" for seq in accessed
    )
    cursor = _advance_to_64_cursor(reader, session_id)
    samples = []
    for _ in range(8):
        accessed.clear()
        started = perf_counter()
        page = reader.load(
            session_id,
            TurnHistoryLoadRequest(direction="before", cursor=cursor, turns=64),
        )
        samples.append((perf_counter() - started) * 1000)
        assert len(accessed) <= 128
        assert all(
            source_by_sequence[seq].semantic_kind != "tool_result" for seq in accessed
        )
    assert [item.ordinal for item in page.items] == list(range(45, 109))
    assert median(samples) < 200
