"""一次性 v1 full-copy 的 loss gate、分段 lineage 和后续 v2 独立复制。"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable

import pytest

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


@pytest.fixture
def fork_source(request: pytest.FixtureRequest, session_bundle_factory) -> Callable:
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"

    def create(records):
        _write_v1_source(
            sessions,
            session_id="source",
            records=records,
            session_bundle_factory=session_bundle_factory,
        )
        session_bundle_factory(sessions, "target")
        session_bundle_factory(sessions, "grandchild")
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


@pytest.mark.parametrize("loss", ["metadata", "unsupported_role", "overlay"])
def test_full_copy_must_not_publish_a_lossy_v1_migration(
    fork_source: Callable, loss: str
) -> None:
    records = _accepted_records()
    if loss == "metadata":
        records[0]["metadata"]["unmapped"] = "must remain in private raw"
    elif loss == "unsupported_role":
        records[1]["role"] = "unknown-role"
    storage = fork_source(records)
    if loss == "overlay":
        with sqlite3.connect(storage.index_path("source")) as connection:
            connection.executescript(
                "CREATE TABLE source_overlays(base TEXT, delta TEXT); INSERT INTO source_overlays VALUES ('base', 'delta');"
            )
    before = artifact_manifest(storage.root("source"))
    with pytest.raises(RuntimeError, match="v1_full_copy_not_lossless"):
        _complete_copy(storage, "source", "target")
    assert not storage.root("target").exists()
    assert artifact_manifest(storage.root("source")) == before
    audit = migration_audits(storage)[0]
    assert audit["status"] == "failed"
    assert audit["result"]["lossless"] is False
    raw = (
        storage.root("target").parent
        / "legacy-import"
        / audit["migration_id"]
        / "source"
    )
    assert artifact_manifest(raw) == before


def test_split_tool_lineage_is_one_to_one_and_v2_grandchild_is_local(
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
    source_before = artifact_manifest(storage.root("source"))
    fork_id = _complete_copy(storage, "source", "target")
    items = storage.read_items("target")
    assert len(items) == 5
    call = next(item for item in items if item.semantic_kind == "tool_call")
    output = next(
        item
        for item in items
        if item.semantic_kind == "assistant_output"
        and item.metadata["legacy_source_ref"]["message_id"] == "legacy-a1"
    )
    with storage._connect("target", "", read_only=True) as connection:
        rows = connection.execute(
            "SELECT source_local_id, target_local_id, source_offset FROM fork_identity_mappings WHERE fork_id=? AND entity_type='item'",
            (fork_id,),
        ).fetchall()
    by_target = {row[1]: row for row in rows}
    assert len({row[0] for row in rows}) == len(rows) == 5
    assert by_target[call.item_id][0].startswith("legacy:v1:message-part:")
    assert json.loads(
        by_target[call.item_id][0].removeprefix("legacy:v1:message-part:")
    ) == {"message_id": "legacy-a1", "part_id": "tool_call:source-call"}
    assert by_target[call.item_id][2] == by_target[output.item_id][2]
    target_before = artifact_manifest(storage.root("target"))
    _complete_copy(storage, "target", "grandchild")
    assert artifact_manifest(storage.root("source")) == source_before
    assert artifact_manifest(storage.root("target")) == target_before
    child_items = storage.read_items("grandchild")
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
    source_root = storage.root("target")
    source_root.rename(source_root.with_name("rollout-archived"))
    restarted = _storage(storage.sessions_dir)
    assert restarted.read_items("grandchild") == child_items
