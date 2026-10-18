"""v1 fork 拒绝、显式迁移和后续 v2 独立复制。"""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.importer import (
    LegacyRolloutImporter,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
TARGET_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"
GRANDCHILD_SESSION_ID = "ses_019bf2257d7e7fd38db6585cdd161690"


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


@pytest.mark.asyncio
async def test_full_copy_rejects_v1_without_migration_side_effects(
    fork_source: Callable, fork_deadlines,
) -> None:
    records = _accepted_records()
    storage = fork_source(records)
    before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    with (
        pytest.raises(FormatDispatchError, match="v1_migration_required"),
        RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage) as saver,
    ):
        await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == before
    assert migration_audits(storage) == []


@pytest.mark.asyncio
async def test_explicit_v1_import_then_v2_full_copy_keeps_tool_lineage_local(
    fork_source: Callable, fork_deadlines,
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
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
        require_lossless=True,
    )
    assert result["status"] == "completed"
    items = storage.storage.read_items(
        TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
    )
    assert len(items) == 5
    call = next(item for item in items if item.semantic_kind == "tool_call")
    target_before = artifact_manifest(storage.storage.root(TARGET_SESSION_ID))
    with RolloutCheckpointSaver(storage.storage.sessions_dir, storage=storage) as saver:
        await copy_rollout_snapshot(
            saver, TARGET_SESSION_ID, GRANDCHILD_SESSION_ID, fork_deadlines=fork_deadlines,
        )
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert artifact_manifest(storage.storage.root(TARGET_SESSION_ID)) == target_before
    child_items = storage.storage.read_items(
        GRANDCHILD_SESSION_ID, storage.storage.main_thread_id(GRANDCHILD_SESSION_ID)
    )
    assert [item.to_dict() for item in child_items] == [item.to_dict() for item in items]
    assert all(not item.item_id.startswith("legacy:v1:") for item in child_items)
    child_call = next(item for item in child_items if item.semantic_kind == "tool_call")
    child_result = next(
        item for item in child_items if item.semantic_kind == "tool_result"
    )
    assert child_call.item_id == call.item_id
    for field in ("tool_call_id", "tool_invocation_id", "tool_attempt_id"):
        assert (
            child_call.payload[field]
            == child_result.payload[field]
            == call.payload[field]
        )
    assert child_call.payload["args"] == {"body": "literal source-call"}
    assert child_result.payload["content"] == "unmodified result body"
    # 删除父节点的运行时 artifact 后仍必须可读；仅移动本测试生成的精确目录。
    source_root = storage.storage.root(TARGET_SESSION_ID)
    source_root.rename(source_root.with_name("rollout-archived"))
    restarted = _storage(storage.storage.sessions_dir).storage
    assert (
        restarted.read_items(
            GRANDCHILD_SESSION_ID, restarted.main_thread_id(GRANDCHILD_SESSION_ID)
        )
        == child_items
    )
