"""v1 fork 拒绝、显式迁移和后续 v2 独立复制。"""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)

SOURCE_SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"
GRANDCHILD_SESSION_ID = "ses_5ce2590d35c74fd9a71e8d7526be328c"


@pytest.fixture
def fork_source(request: pytest.FixtureRequest, session_bundle_factory) -> Callable:
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"

    def create(records):
        _write_v1_source(
            sessions,
            session_id=SOURCE_SESSION_ID,
            records=records,
            session_bundle_factory=session_bundle_factory,
        )
        session_bundle_factory(sessions, TARGET_SESSION_ID)
        session_bundle_factory(sessions, GRANDCHILD_SESSION_ID)
        return _storage(sessions)

    return create


def _complete_copy(storage, source: str, target: str) -> str:
    view = storage.clone_rollout(
        source_thread_id=source, target_thread_id=target, source_checkpoint_id=None
    )
    fields = {
        "target_session_id": target,
        "source_session_id": source,
        "source_checkpoint_id": None,
        "source_view_id": view,
        "fork_mode": "full_rollout_copy",
        "relationship": "detached",
    }
    materialization, fork_id = storage.begin_fork_materialization(**fields)
    assert storage.commit_fork_materialization(materialization, **fields) == fork_id
    return fork_id


def test_full_copy_rejects_v1_without_migration_side_effects(
    fork_source: Callable,
) -> None:
    records = _accepted_records()
    storage = fork_source(records)
    before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    with pytest.raises(FormatDispatchError, match="v1_migration_required"):
        storage.clone_rollout(
            source_thread_id=SOURCE_SESSION_ID,
            target_thread_id=TARGET_SESSION_ID,
            source_checkpoint_id=None,
        )
    assert not storage.root(TARGET_SESSION_ID).exists()
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == before
    assert migration_audits(storage) == []


def test_explicit_v1_import_then_v2_full_copy_keeps_tool_lineage_local(
    fork_source: Callable,
) -> None:
    records = _accepted_records()
    final = copy.deepcopy(records[1])
    final.update(message_sequence=4, message_id="final")
    final["message"]["data"]["id"] = "final"
    records[1]["metadata"] = {}
    records[1]["message"]["data"]["tool_calls"] = [
        {
            "id": "source-call",
            "name": "echo",
            "args": {"body": "literal source-call"},
            "type": "tool_call",
        }
    ]
    records.extend(
        [
            {
                "format_version": 1,
                "record_type": "message",
                "message_sequence": 3,
                "message_id": "source-result",
                "turn_id": "legacy-turn-1",
                "role": "tool",
                "message": {
                    "type": "tool",
                    "data": {
                        "type": "tool",
                        "content": "unmodified result body",
                        "id": "source-result",
                        "tool_call_id": "source-call",
                        "name": "echo",
                        "status": "success",
                    },
                },
                "metadata": {},
            },
            final,
        ]
    )
    storage = fork_source(records)
    source_before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    result = storage.migrate_legacy_to_v2(
        SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID, require_lossless=True
    )
    assert result["status"] == "completed"
    items = storage.read_items(TARGET_SESSION_ID)
    assert len(items) == 5
    call = next(item for item in items if item.semantic_kind == "tool_call")
    target_before = artifact_manifest(storage.root(TARGET_SESSION_ID))
    fork_id = _complete_copy(storage, TARGET_SESSION_ID, GRANDCHILD_SESSION_ID)
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == source_before
    assert artifact_manifest(storage.root(TARGET_SESSION_ID)) == target_before
    child_items = storage.read_items(GRANDCHILD_SESSION_ID)
    with storage._connect(GRANDCHILD_SESSION_ID, "", read_only=True) as connection:
        rows = connection.execute(
            "SELECT source_local_id, target_local_id FROM fork_identity_mappings WHERE fork_id=? AND entity_type='item'",
            (fork_id,),
        ).fetchall()
    assert len(rows) == len(items) == len(child_items)
    assert {row[0] for row in rows} == {item.item_id for item in items}
    assert {row[1] for row in rows} == {item.item_id for item in child_items}
    assert all(not row[0].startswith("legacy:v1:") for row in rows)
    child_call = next(item for item in child_items if item.semantic_kind == "tool_call")
    child_result = next(
        item for item in child_items if item.semantic_kind == "tool_result"
    )
    assert child_call.item_id not in {item.item_id for item in items}
    for field in ("tool_call_id", "tool_invocation_id", "tool_attempt_id"):
        assert (
            child_call.payload[field]
            == child_result.payload[field]
            != call.payload[field]
        )
    assert child_call.payload["args"] == {"body": "literal source-call"}
    assert child_result.payload["content"] == "unmodified result body"
    # 删除父节点的运行时 artifact 后仍必须可读；仅移动本测试生成的精确目录。
    source_root = storage.root(TARGET_SESSION_ID)
    source_root.rename(source_root.with_name("rollout-archived"))
    restarted = _storage(storage.sessions_dir)
    assert restarted.read_items(GRANDCHILD_SESSION_ID) == child_items
