"""完整 fork 的 activation 原身份、原正文和目标本地冷读取。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)
from tests.unit.services.infrastructure.rollout_context.test_resource_activation_retention import (
    SESSION_ID,
    _sealed_with_activation,
)

TARGET_SESSION_ID = "ses_019c49da91667f7b842224c1bfc58b01"


@pytest.mark.asyncio
async def test_full_copy_preserves_original_activation_identity_and_local_bodies(
    tmp_path, session_bundle_factory, fork_deadlines,
):
    sessions = tmp_path / "sessions"
    for session in (SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions, session)
    saver, original, sealed = _sealed_with_activation(sessions)
    source_thread = saver._storage.storage.main_thread_id(SESSION_ID)
    target_thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    with saver._storage.storage.read_connection(SESSION_ID, source_thread, "") as connection:
        original_rows = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("resource_activation_snapshots", "resource_activation_assembly_bindings")
        }
    await copy_rollout_snapshot(saver, SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    with saver._storage.storage.read_connection(TARGET_SESSION_ID, target_thread, "") as connection:
        for table, rows in original_rows.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
        assert connection.execute("SELECT session_id,thread_id FROM history_namespace_bindings").fetchall() == [(SESSION_ID, source_thread)]
    source_root = saver._storage.storage.root(SESSION_ID, thread_id=source_thread)
    source_root.rename(source_root.with_name("source-offline"))
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.load_sealed_resource_activation(
            TARGET_SESSION_ID, thread_id=target_thread, assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
        )
        assert restored.to_dict() == original.to_dict()
        assert restored.owner_session_id == SESSION_ID and restored.owner_thread_id == source_thread
        for left, right in zip(original.bindings, restored.bindings, strict=True):
            assert right.members == left.members and right.detail_ref == left.detail_ref
            assert restarted.read_resource_activation_payload(restored, right, storage_owner=(TARGET_SESSION_ID, target_thread)) == {"payload": "resource-body"}
        with pytest.raises(ValueError, match="turn_not_resumable"):
            restarted.dispatch_replay(TARGET_SESSION_ID, thread_id=target_thread, turn_id=restored.turn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("missing", "hash"))
async def test_full_copy_rewind_rejects_invalid_historical_scope_without_writes(
    tmp_path, session_bundle_factory, fork_deadlines, corruption,
):
    sessions = tmp_path / "sessions"
    for session in (SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions, session)
    saver, _original, _sealed = _sealed_with_activation(sessions)
    source_thread = saver._storage.storage.main_thread_id(SESSION_ID)
    target_thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    await copy_rollout_snapshot(
        saver, SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines,
    )
    source_root = saver._storage.storage.root(SESSION_ID, thread_id=source_thread)
    source_root.rename(source_root.with_name("source-offline"))
    target_index = saver._storage.storage.index_path(TARGET_SESSION_ID, "", thread_id=target_thread)
    target_jsonl = saver._storage.storage.jsonl_path(TARGET_SESSION_ID, "", thread_id=target_thread)
    with closing(sqlite3.connect(target_index)) as connection:
        if corruption == "missing":
            connection.execute("DELETE FROM history_namespace_bindings")
        else:
            connection.execute("UPDATE history_namespace_bindings SET binding_hash='invalid'")
        connection.commit()
        before = tuple(connection.iterdump())
    jsonl_before = target_jsonl.read_bytes()

    with (
        RolloutCheckpointSaver(sessions) as restarted,
        session_thread_owner_scope(
            MutationIntentOwner(TARGET_SESSION_ID, target_thread), langgraph_thread_id=target_thread,
        ),
        pytest.raises(ValueError, match="source-mismatch"),
    ):
        restarted._storage.checkpoints.rewind_to_checkpoint(
            thread_id=target_thread, checkpoint_id="cp-1", anchor_mode="inclusive",
        )

    with closing(sqlite3.connect(target_index)) as connection:
        assert tuple(connection.iterdump()) == before
    assert target_jsonl.read_bytes() == jsonl_before
