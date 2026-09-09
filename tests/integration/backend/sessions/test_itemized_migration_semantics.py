"""v1 role/window、tool identity 与显式 manifest 证据的集成验收。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Callable

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.migration.store import (
    LegacyMigrationStorage,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    prepare_migration_workspace,
)


@pytest.fixture
def migration_setup(request: pytest.FixtureRequest, session_bundle_factory) -> Callable:
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"

    def setup(records: list[dict[str, object]]) -> LegacyMigrationStorage:
        _write_v1_source(
            sessions,
            session_id="source",
            session_bundle_factory=session_bundle_factory,
            records=records,
        )
        session_bundle_factory(sessions, "target")
        return _storage(sessions)

    return setup


@pytest.mark.parametrize("role", ["tool", "function"])
@pytest.mark.parametrize(
    "outcome,status,marker",
    [
        ("success", "completed", "success"),
        ("error", "failed", "unknown"),
        (None, "unknown", "unknown"),
    ],
)
def test_tool_calls_and_results_get_target_local_linked_identity(
    migration_setup: Callable,
    role: str,
    outcome: str | None,
    status: str,
    marker: str,
) -> None:
    records = _accepted_records()
    final = copy.deepcopy(records[1])
    final.update(message_sequence=4, message_id="legacy-final")
    final["message"]["data"]["id"] = "legacy-final"
    records[1]["metadata"] = {}
    records[1]["message"]["data"]["tool_calls"] = [
        {"id": "source-call", "name": "echo", "args": {"value": 2}, "type": "tool_call"}
    ]
    result = {
        "format_version": 1,
        "record_type": "message",
        "message_sequence": 3,
        "message_id": "source-result",
        "turn_id": "legacy-turn-1",
        "role": role,
        "message": {
            "type": role,
            "data": {
                "type": role,
                "content": "result",
                "id": "source-result",
                "tool_call_id": "source-call",
                "name": None,
                "status": outcome,
            },
        },
        "metadata": {},
    }
    storage = migration_setup([*records, result, final])
    imported = storage.migrate_legacy_to_v2(
        "source", target_thread_id="target", require_lossless=True
    )
    items = storage.read_items("target")
    call = next(item for item in items if item.semantic_kind == "tool_call")
    result_item = next(item for item in items if item.semantic_kind == "tool_result")
    root = next(item for item in items if item.semantic_kind == "user_input")
    assert root.producer_ref["producer_kind"] == "user"
    assert call.producer_ref["producer_kind"] == "provider"
    assert result_item.producer_ref["producer_kind"] == "tool"
    assert (
        call.payload["tool_call_id"]
        == result_item.payload["tool_call_id"]
        != "source-call"
    )
    assert (
        call.payload["tool_invocation_id"] == result_item.payload["tool_invocation_id"]
    )
    assert call.payload["tool_attempt_id"] == result_item.payload["tool_attempt_id"]
    assert result_item.payload["result_id"] != "source-result"
    assert result_item.payload["name"] == "echo"
    assert (result_item.status, result_item.payload["tool_outcome"]) == (status, marker)
    assert call.metadata["legacy_source_ref"]["part_id"] == "tool_call:source-call"
    assert imported["migrated"][0]["status"] == "completed"
    assert len({item.item_id for item in items}) == len(items) == 5


@pytest.mark.parametrize(
    "case,expected",
    [
        ("different_ids", "legacy_turn_group_ambiguous"),
        ("multiple_roots", "legacy_multiple_user_messages"),
        ("duplicate_message_id", "legacy_identity_conflict"),
        ("unsupported_same_id", "legacy_multiple_user_messages"),
    ],
)
def test_conflicting_candidates_never_create_turns(
    migration_setup: Callable, case: str, expected: str
) -> None:
    records = _accepted_records()
    if case == "different_ids":
        records[1]["turn_id"] = "different"
    elif case == "duplicate_message_id":
        records[1]["message_id"] = records[0]["message_id"]
        records[1]["message"]["data"]["id"] = records[0]["message_id"]
    else:
        second = copy.deepcopy(records[0])
        second.update(message_sequence=3, message_id="user-2")
        second["message"]["data"]["id"] = "user-2"
        records.append(second)
        if case == "unsupported_same_id":
            records[1]["role"] = "unknown-role"
    storage = migration_setup(records)
    result = storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert not result["migrated"]
    assert {candidate["candidate_status"] for candidate in result["rejected"]} == {
        expected
    }
    assert sum(len(candidate["records"]) for candidate in result["rejected"]) == len(
        records
    )
    assert storage.read_items("target") == []


@pytest.mark.parametrize("trusted", [True, False])
def test_system_reminder_never_becomes_a_turn_member(
    migration_setup: Callable, trusted: bool
) -> None:
    records = _accepted_records()
    notice = copy.deepcopy(records[0])
    notice.update(message_sequence=3, message_id="notice", role="system_reminder")
    notice["message"]["data"]["id"] = "notice"
    notice["metadata"] = {"internal": True, "checkpoint": True} if trusted else {}
    storage = migration_setup([*records, notice])
    result = storage.migrate_legacy_to_v2("source", target_thread_id="target")
    if trusted:
        item = storage.read_items("target")[-1]
        assert (item.semantic_kind, item.turn_id, item.turn_scope) == (
            "runtime_notice",
            None,
            "pending_next_turn",
        )
    else:
        assert result["rejected"][0]["candidate_status"] == "legacy_unsupported_role"
        assert storage.read_items("target") == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_manifest_final_pointer_is_validated(
    migration_setup: Callable, corrupt: bool
) -> None:
    records = _accepted_records()
    records[1]["metadata"] = {}
    storage = migration_setup(records)
    with sqlite3.connect(storage.index_path("source")) as connection:
        connection.execute(
            "CREATE TABLE turns(turn_id TEXT,status TEXT,final_message_id TEXT,final_message_sequence INTEGER)"
        )
        connection.execute(
            "INSERT INTO turns VALUES (?, 'completed', 'legacy-a1', 2)",
            ("wrong-turn" if corrupt else "legacy-turn-1",),
        )
    if corrupt:
        with pytest.raises(FormatDispatchError, match="final pointer"):
            storage.migrate_legacy_to_v2("source", target_thread_id="target")
        assert not storage.root("target").exists()
    else:
        result = storage.migrate_legacy_to_v2("source", target_thread_id="target")
        assert result["migrated"][0]["status"] == "completed"
        assert result["migrated"][0]["final_item_id"] is not None


@pytest.mark.parametrize("corruption", [None, "hash", "length", "role"])
def test_legacy_manifest_content_identity_is_checked(
    migration_setup: Callable, corruption: str | None
) -> None:
    records = _accepted_records()
    storage = migration_setup(records)
    with sqlite3.connect(storage.index_path("source")) as connection:
        connection.executescript(
            "ALTER TABLE messages ADD COLUMN role TEXT; ALTER TABLE messages ADD COLUMN content_hash TEXT; ALTER TABLE messages ADD COLUMN content_length INTEGER;"
        )
        for record in records:
            body = json.dumps(
                record["message"]["data"]["content"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            connection.execute(
                "UPDATE messages SET role=?, content_hash=?, content_length=? WHERE message_sequence=?",
                (
                    record["role"],
                    hashlib.sha256(body).hexdigest(),
                    len(body),
                    record["message_sequence"],
                ),
            )
        if corruption:
            query = {
                "hash": "UPDATE messages SET content_hash='invalid' WHERE message_sequence=1",
                "length": "UPDATE messages SET content_length=content_length-1 WHERE message_sequence=1",
                "role": "UPDATE messages SET role='assistant' WHERE message_sequence=1",
            }[corruption]
            connection.execute(query)
    if corruption:
        with pytest.raises(
            FormatDispatchError, match="content_hash|content_length|role"
        ):
            storage.migrate_legacy_to_v2("source", target_thread_id="target")
        assert not storage.root("target").exists()
    else:
        assert (
            storage.migrate_legacy_to_v2("source", target_thread_id="target")[
                "lossless"
            ]
            is True
        )
