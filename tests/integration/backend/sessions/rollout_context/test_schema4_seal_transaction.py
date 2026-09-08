"""正式 schema4 的单事务封存、失败回滚与重启恢复。"""

import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db,
)

__all__ = ["registry_db"]


@pytest.fixture
def plan(registry_db):
    _saver, session, _connection, _accepted = registry_db
    return ContextRequestPlan(
        session_id=session,
        plan_id="transaction-plan",
        refs=(),
        plan_creation_idempotency_key="transaction-create",
        tool_set_refs=(
            ToolSetRef.from_tool_snapshot(
                session_id=session,
                snapshot_id="tool-read",
                plan_id="transaction-plan",
                source_revision="tool-revision",
                tools=({"name": "read_file", "parameters": {"type": "object"}},),
            ),
        ),
    )


@pytest.fixture
def snapshot_factory(registry_db, plan):
    _saver, session, _connection, accepted = registry_db

    def build(**overrides):
        fields = {
            "plan": plan,
            "assembly_id": "transaction-assembly",
            "session_id": session,
            "turn_id": str(accepted["turn_id"]),
            "execution_id": str(accepted["initial_execution_id"]),
            "provider_version": "transaction-provider",
            "target_format": "responses",
        }
        fields.update(overrides)
        return ContextPlanComposer().assembly(**fields)

    return build


def test_real_seal_commits_plan_tools_selection_once_and_reopens(
    registry_db, plan, snapshot_factory
):
    saver, session, connection, _accepted = registry_db
    original = saver._storage.jsonl_path(session).read_bytes()
    saver.create_context_plan(session, plan)
    snapshot = snapshot_factory()
    commit_id = saver._storage.seal_context_assembly(
        snapshot,
        seal_idempotency_key="transaction-seal",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans"
    ).fetchall() == [("sealed", snapshot.assembly_id)]
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots"
    ).fetchall() == [(snapshot.assembly_id,)]
    assert connection.execute(
        "SELECT assembly_id, plan_ordinal, included FROM context_assembly_selections"
    ).fetchall() == [(snapshot.assembly_id, 0, 1)]
    assert connection.execute(
        "SELECT commit_kind, commit_mode, jsonl_offset_before, jsonl_offset_after "
        "FROM storage_commits WHERE commit_id=?",
        (commit_id,),
    ).fetchone() == ("assembly_sealed", "metadata_only", len(original), len(original))
    restarted = RolloutCheckpointSaver(saver._storage.sessions_dir)
    restored = restarted._storage.get_context_assembly(
        session, assembly_id=snapshot.assembly_id
    )
    assert restored.to_dict() == snapshot.to_dict()
    assert (
        restarted._storage.seal_context_assembly(
            restored,
            seal_idempotency_key="transaction-seal",
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
        == commit_id
    )
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (1,)
    assert saver._storage.jsonl_path(session).read_bytes() == original


def test_unregistered_snapshot_cannot_create_dispatchable_rows(
    registry_db, snapshot_factory
):
    saver, _session, connection, _accepted = registry_db
    with pytest.raises(KeyError, match="context plan 不存在"):
        saver._storage.seal_context_assembly(
            snapshot_factory(),
            seal_idempotency_key="unregistered",
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    for table in ("context_plans", "context_assemblies", "context_assembly_selections"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["selection", "binding", "commit"])
def test_failure_rolls_back_assembly_selection_binding_and_commit(
    registry_db, plan, snapshot_factory, monkeypatch, failure
):
    saver, session, connection, _accepted = registry_db
    saver.create_context_plan(session, plan)
    original = saver._storage.jsonl_path(session).read_bytes()
    before_commits = connection.execute("SELECT * FROM storage_commits").fetchall()
    if failure == "commit":

        def refuse_commit(candidate):
            assert candidate.execute(
                "SELECT plan_state FROM context_plans"
            ).fetchone() == ("sealed",)
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(saver._storage, "_commit_connection", refuse_commit)
    else:
        event, table = (
            ("INSERT", "context_assembly_selections")
            if failure == "selection"
            else ("UPDATE", "context_plans")
        )
        connection.execute(
            f"CREATE TRIGGER refuse_seal BEFORE {event} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected seal failure'); END"
        )
        connection.commit()
    with pytest.raises(Exception, match="injected"):
        saver._storage.seal_context_assembly(
            snapshot_factory(),
            seal_idempotency_key="transaction-seal",
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert (
        connection.execute("SELECT * FROM storage_commits").fetchall() == before_commits
    )
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans"
    ).fetchone() == ("unsealed", None)
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots"
    ).fetchone() == (None,)
    for table in (
        "context_assemblies",
        "context_assembly_selections",
        "assembly_item_refs",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert saver._storage.jsonl_path(session).read_bytes() == original


def test_missing_draft_tool_registry_is_not_recreated_at_seal(
    registry_db, plan, snapshot_factory
):
    saver, session, connection, _accepted = registry_db
    saver.create_context_plan(session, plan)
    with connection:
        connection.execute("DELETE FROM tool_set_snapshots")
    with pytest.raises(ValueError, match="source-mismatch"):
        saver._storage.seal_context_assembly(
            snapshot_factory(),
            seal_idempotency_key="transaction-seal",
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert connection.execute("SELECT count(*) FROM tool_set_snapshots").fetchone() == (
        0,
    )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        0,
    )


def test_seal_retry_input_hash_is_bound_to_the_committed_snapshot(
    registry_db, plan, snapshot_factory
):
    saver, session, connection, _accepted = registry_db
    saver.create_context_plan(session, plan)
    snapshot = snapshot_factory()
    saver._storage.seal_context_assembly(
        snapshot, seal_idempotency_key="transaction-seal",
        seal_input_hash=sha256_jcs("original-request-input"),
    )
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver._storage.seal_context_assembly(
            snapshot, seal_idempotency_key="transaction-seal",
            seal_input_hash=sha256_jcs("changed-request-input"),
        )
    with connection:
        connection.execute(
            "UPDATE context_plans SET seal_input_hash=?",
            (sha256_jcs("changed-request-input"),),
        )
    with pytest.raises(ValueError, match="source-mismatch"):
        saver.get_context_plan_registration(session, plan_id=plan.plan_id)
    with pytest.raises(ValueError, match="source-mismatch"):
        saver._storage.get_context_assembly(session, assembly_id=snapshot.assembly_id)


@pytest.mark.parametrize("input_hash", ["", "not-a-hash", "sha256:jcs:v1:" + "A" * 64])
def test_invalid_seal_input_hash_rejected_before_any_binding(
    registry_db, plan, snapshot_factory, input_hash
):
    saver, session, connection, _accepted = registry_db
    saver.create_context_plan(session, plan)
    with pytest.raises(ValueError, match="seal_input_hash"):
        saver._storage.seal_context_assembly(
            snapshot_factory(), seal_idempotency_key="transaction-seal",
            seal_input_hash=input_hash,
        )
    assert connection.execute("SELECT plan_state FROM context_plans").fetchone() == ("unsealed",)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)


@pytest.mark.parametrize("change", ["key", "assembly", "provider"])
def test_sealed_plan_cannot_be_reused_with_changed_input(
    registry_db, plan, snapshot_factory, change
):
    saver, session, connection, _accepted = registry_db
    saver.create_context_plan(session, plan)
    snapshot = snapshot_factory()
    saver._storage.seal_context_assembly(
        snapshot,
        seal_idempotency_key="transaction-seal",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    candidate = (
        snapshot_factory(assembly_id="second-assembly")
        if change == "assembly"
        else snapshot_factory(provider_version="second-provider")
        if change == "provider"
        else snapshot
    )
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver._storage.seal_context_assembly(
            candidate,
            seal_idempotency_key="second-key"
            if change == "key"
            else "transaction-seal",
            seal_input_hash=sha256_jcs("storage-seal-test-input"),
        )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        1,
    )
    with pytest.raises(ValueError, match="不可修订"):
        saver.revise_context_plan(
            session, replace(plan, history_view_revision=8), expected_revision=0
        )


@pytest.mark.parametrize("crash_point", ["before_commit", "after_commit"])
def test_process_exit_preserves_single_atomic_plan_binding(
    registry_db, plan, snapshot_factory, crash_point, request
):
    saver, session, connection, accepted = registry_db
    saver.create_context_plan(session, plan)
    original = saver._storage.jsonl_path(session).read_bytes()
    # 真实子进程退出，不通过 Python 异常路径触发连接 context manager 回滚。
    database_path = connection.execute("PRAGMA database_list").fetchone()[2]
    connection.close()
    program = """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.runtime.composer import ContextPlanComposer
from app.domain.itemized.hashing import sha256_jcs
saver = RolloutCheckpointSaver(sys.argv[1])
session = sys.argv[2]
plan = saver.get_context_plan_registration(session, plan_id='transaction-plan').draft
snapshot = ContextPlanComposer().assembly(
    plan=plan, assembly_id='transaction-assembly', session_id=session,
    turn_id=sys.argv[3], execution_id=sys.argv[4],
    provider_version='transaction-provider', target_format='responses',
)
commit = saver._storage._commit_connection
def crash(connection):
    assert connection.execute('SELECT plan_state FROM context_plans').fetchone() == ('sealed',)
    assert connection.execute('SELECT count(*) FROM context_assembly_selections').fetchone() == (1,)
    if sys.argv[5] == 'after_commit':
        commit(connection)
    os._exit(66)
saver._storage._commit_connection = crash
saver._storage.seal_context_assembly(snapshot, seal_idempotency_key='transaction-seal', seal_input_hash=sha256_jcs('storage-seal-test-input'))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(saver._storage.sessions_dir),
            session,
            str(accepted["turn_id"]),
            str(accepted["initial_execution_id"]),
            crash_point,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 66, result.stdout + result.stderr
    restarted = RolloutCheckpointSaver(saver._storage.sessions_dir)
    connection = sqlite3.connect(database_path)
    request.addfinalizer(connection.close)
    registration = restarted.get_context_plan_registration(
        session, plan_id=plan.plan_id
    )
    if crash_point == "before_commit":
        assert registration.plan_state == "unsealed"
        assert registration.assembly_id is None
        assert connection.execute(
            "SELECT count(*) FROM context_assemblies"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT assembly_id FROM tool_set_snapshots"
        ).fetchone() == (None,)
    else:
        assert registration.plan_state == "sealed"
        assert registration.assembly_id == "transaction-assembly"
    # 不论是否跨过 COMMIT，显式重试都只留下同一个 seal commit。
    restarted._storage.seal_context_assembly(
        snapshot_factory(),
        seal_idempotency_key="transaction-seal",
        seal_input_hash=sha256_jcs("storage-seal-test-input"),
    )
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        1,
    )
    assert restarted._storage.jsonl_path(session).read_bytes() == original
