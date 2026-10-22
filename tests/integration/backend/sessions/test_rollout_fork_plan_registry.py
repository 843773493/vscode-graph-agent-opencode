"""fork 原样保留真实 plan registry；历史只读，新目标可创建自己的计划。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions import test_rollout_fork_protected
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    SOURCE_SESSION_ID,
    TARGET_SESSION_ID,
)

fork_workspace = test_rollout_fork_protected.fork_workspace
protected_source = test_rollout_fork_protected.protected_source


@pytest.fixture
def draft_factory(protected_source):
    saver = protected_source[0]
    thread = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)

    def create(plan_id, *, tool_name="read_file"):
        tool = ToolSetRef.from_tool_snapshot(
            session_id=SOURCE_SESSION_ID, thread_id=thread, plan_id=plan_id,
            snapshot_id="same-local-tools", source_revision=f"{tool_name}-revision",
            tools=({"name": tool_name, "parameters": {"type": "object", "properties": {"api_key": {"type": "string"}}}},),
        )
        draft = ContextRequestPlan(
            session_id=SOURCE_SESSION_ID, thread_id=thread, plan_id=plan_id, refs=(),
            tool_set_refs=(tool,), plan_creation_idempotency_key=f"create-{plan_id}",
        )
        saver.create_context_plan(SOURCE_SESSION_ID, draft)
        return draft
    return create


@pytest.mark.asyncio
async def test_full_copy_preserves_true_registry_rows_and_rejects_historical_writes(
    protected_source, draft_factory, fork_deadlines,
):
    saver, sealed, _, _ = protected_source
    source_thread = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    target_thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    initial = draft_factory("real-draft")
    current = replace(initial, tool_set_refs=(), history_view_revision=2)
    saver.revise_context_plan(SOURCE_SESSION_ID, current, expected_revision=0)
    tables = ("context_plans", "context_plan_refs", "context_plan_contributions", "tool_set_snapshots")
    with saver._storage.storage.read_connection(SOURCE_SESSION_ID, source_thread, "") as connection:
        originals = {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
        original_draft = read_registration(connection, SOURCE_SESSION_ID, source_thread, initial.plan_id)
        original_sealed = read_registration(connection, SOURCE_SESSION_ID, source_thread, sealed.plan_id)
    await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    with saver._storage.storage.read_connection(TARGET_SESSION_ID, target_thread, "") as connection:
        for table in tables:
            assert connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == originals[table]
        assert read_registration(connection, SOURCE_SESSION_ID, source_thread, initial.plan_id) == original_draft
        assert read_registration(connection, SOURCE_SESSION_ID, source_thread, sealed.plan_id) == original_sealed
        creation, draft = connection.execute("SELECT creation_json,draft_json FROM context_plans WHERE plan_id=?", (initial.plan_id,)).fetchone()
        assert json.loads(creation)["tool_set_refs"][0]["ref_id"] == "same-local-tools"
        assert json.loads(draft)["tool_set_refs"] == []
        assert original_draft.revision == 1 and original_draft.plan_state == "unsealed"
        assert original_sealed.plan_state == "sealed" and original_sealed.seal_input_hash is not None
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    for write in (
        lambda: saver.create_context_plan(TARGET_SESSION_ID, current),
        lambda: saver.revise_context_plan(TARGET_SESSION_ID, current, expected_revision=1),
        lambda: saver.seal_context_plan(
            TARGET_SESSION_ID, target_thread, current, wire_profile=None, wire_params=None,
            turn_id="source-turn", execution_id="protected-source-execution",
            seal_idempotency_key="reject-historical-seal", provider_version="historical",
        ),
    ):
        with pytest.raises(ValueError, match="source-mismatch"):
            write()
    native = ContextRequestPlan(
        session_id=TARGET_SESSION_ID, thread_id=target_thread, plan_id="target-native-plan",
        refs=(), tool_set_refs=(), plan_creation_idempotency_key="target-native-create",
    )
    assert saver.create_context_plan(TARGET_SESSION_ID, native).draft == native
    source_root = saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread)
    source_root.rename(source_root.with_name("source-offline"))
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        assert restarted.get_context_plan_registration(TARGET_SESSION_ID, thread_id=target_thread, plan_id=initial.plan_id) == original_draft
        assert restarted.get_context_plan_registration(TARGET_SESSION_ID, thread_id=target_thread, plan_id=sealed.plan_id) == original_sealed
        snapshot = restarted.get_context_assembly(TARGET_SESSION_ID, target_thread, assembly_id=sealed.assembly_id)
        assert snapshot.to_dict() == sealed.to_dict()


@pytest.mark.asyncio
async def test_full_copy_same_tool_id_stays_scoped_to_original_plans(protected_source, draft_factory, fork_deadlines):
    saver = protected_source[0]
    drafts = (draft_factory("draft-a", tool_name="read_file"), draft_factory("draft-b", tool_name="write_file"))
    await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    values = [saver.get_context_plan_registration(TARGET_SESSION_ID, thread_id=thread, plan_id=plan.plan_id).draft for plan in drafts]
    assert values == list(drafts)
    assert values[0].plan_id != values[1].plan_id
    assert values[0].tool_set_refs[0].ref_id == values[1].tool_set_refs[0].ref_id == "same-local-tools"
    assert values[0].tool_set_refs[0].tools != values[1].tool_set_refs[0].tools
    assert all(value.tool_set_refs[0].assembly_id is None for value in values)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["historical_source", "foreign"])
async def test_cold_target_snapshot_and_plan_reject_wrong_database_owner(protected_source, owner: str, fork_deadlines):
    saver, sealed, _, _ = protected_source
    await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    storage = saver._storage.storage
    target_thread = storage.main_thread_id(TARGET_SESSION_ID)
    assert saver.get_context_plan_registration(
        TARGET_SESSION_ID, thread_id=target_thread, plan_id=sealed.plan_id
    ).draft.session_id == SOURCE_SESSION_ID
    changed_owner = (
        (SOURCE_SESSION_ID, storage.main_thread_id(SOURCE_SESSION_ID))
        if owner == "historical_source"
        else (f"ses_{create_uuid_hex()}", f"thr_{create_uuid_hex()}")
    )
    with storage._connect(target_thread, "", session_id=TARGET_SESSION_ID) as connection:
        connection.execute("UPDATE database_meta SET session_id=?,thread_id=?", changed_owner)
    jsonl = storage.jsonl_path(TARGET_SESSION_ID, thread_id=target_thread).read_bytes()
    with RolloutCheckpointSaver(storage.sessions_dir) as restarted:
        for read in (
            lambda: restarted._storage.storage.open_read_snapshot(TARGET_SESSION_ID, target_thread),
            lambda: restarted.get_context_plan_registration(
                TARGET_SESSION_ID, thread_id=target_thread, plan_id=sealed.plan_id
            ),
        ):
            with pytest.raises(ValueError, match="读取方与目标 rollout catalog owner 不一致"):
                read()
    with storage._connect(target_thread, "", session_id=TARGET_SESSION_ID, read_only=True) as connection:
        assert connection.execute("SELECT session_id,thread_id FROM database_meta").fetchone() == changed_owner
    assert storage.jsonl_path(TARGET_SESSION_ID, thread_id=target_thread).read_bytes() == jsonl


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["creation_hash", "draft_hash", "seal_input_hash"])
async def test_cold_history_rejects_tampered_original_registry_hash(protected_source, column, fork_deadlines):
    saver, sealed, _, _ = protected_source
    await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID, fork_deadlines=fork_deadlines)
    thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    with saver._storage.storage._connect(thread, "", session_id=TARGET_SESSION_ID) as connection:
        connection.execute(f"UPDATE context_plans SET {column}=? WHERE plan_id=?", (sha256_jcs({"tampered": column}), sealed.plan_id))
    with (
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted,
        pytest.raises(ValueError, match="source-mismatch"),
    ):
        restarted.get_context_plan_registration(TARGET_SESSION_ID, thread_id=thread, plan_id=sealed.plan_id)
