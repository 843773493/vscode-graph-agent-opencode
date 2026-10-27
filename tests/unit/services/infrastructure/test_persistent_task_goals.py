"""Goal 使用唯一任务 owner，旧文件只允许显式一次性导入。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.goal import (
    GoalJobAccountingDTO,
    GoalStatus,
    SessionGoalDTO,
)
from app.services.infrastructure.persistent_tasks.goal_import import import_legacy_goals
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING
from tests.unit.core.catalog_workspace_helper import (
    build_catalog_workspace,
    make_session_metadata,
)


@pytest.fixture
async def goal_case(empty_workspace_scope: tuple[str, Path]):
    with build_catalog_workspace(empty_workspace_scope[1].parent) as workspace:
        session_id = await workspace.create_session("迁移目标")
        with WorkspaceStateStore(workspace_root=workspace.workspace_root) as state:
            store = PersistentTaskStore(state, workspace_id=workspace.workspace_id, paths=workspace.resolver)
            now = datetime(2026, 10, 27, tzinfo=UTC)
            goal = SessionGoalDTO(goal_id="goal_legacy", session_id=session_id,
                                  thread_id=workspace.resolver.main_thread_id(session_id), objective="原目标",
                                  status=GoalStatus.active, token_budget=100, tokens_used=7, time_used_seconds=3,
                                  revision=9, created_at=now, updated_at=now, last_accounted_job_id="job_a",
                                  last_continued_job_id="idle:original", accounted_jobs={
                                      "job_a": GoalJobAccountingDTO(tokens=7, elapsed_seconds=3, time_closed=True),
                                  })
            yield workspace, state, store, goal


def _legacy(workspace, goal: SessionGoalDTO) -> Path:
    path = workspace.session_dir(goal.session_id) / "goal.json"
    path.write_text(goal.model_dump_json(exclude={"thread_id"}), encoding="utf-8")
    return path


async def test_goal_uses_task_row_and_reuses_unchanged_definition(goal_case):
    workspace, state, store, goal = goal_case
    store.goal_write(goal)
    assert store.goal_read(goal.session_id) == goal
    assert store.goal_list() == [goal]
    assert not (workspace.session_dir(goal.session_id) / "goal.json").exists()
    accounted = goal.model_copy(update={"tokens_used": 9})
    store.goal_write(accounted)
    with closing(state.connection()) as connection:
        row = connection.execute("SELECT application_json,definition_revision FROM persistent_tasks WHERE task_id=?", (goal.goal_id,)).fetchone()
        assert "thread_id" not in json.loads(row[0]) and row[1] == 1
        assert connection.execute("SELECT COUNT(*) FROM persistent_task_definitions WHERE task_id=?", (goal.goal_id,)).fetchone()[0] == 1
    changed = accounted.model_copy(update={"objective": "新目标", "revision": 10})
    store.goal_write(changed)
    assert store.goal_read(goal.session_id) == changed
    with pytest.raises(ValueError, match="权威 main"):
        store.goal_write(changed.model_copy(update={"thread_id": "thr_child"}))
    assert store.goal_read(goal.session_id) == changed


async def test_import_preserves_exact_state_and_does_not_overwrite_after_unlink_failure(goal_case, monkeypatch):
    workspace, state, store, goal = goal_case
    path = _legacy(workspace, goal)
    unlink = Path.unlink

    def fail_unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("migration-unlink-fault")
        return unlink(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(PermissionError, match="migration-unlink-fault"):
            import_legacy_goals(store)
    assert store.goal_read(goal.session_id) == goal
    edited = goal.model_copy(update={"objective": "提交后用户修改", "revision": 10, "status": GoalStatus.paused})
    store.goal_write(edited)
    cold = PersistentTaskStore(state, workspace_id=workspace.workspace_id, paths=workspace.resolver)
    assert import_legacy_goals(cold) == 1
    assert not path.exists()
    assert cold.goal_read(goal.session_id) == edited
    assert import_legacy_goals(cold) == 0


@pytest.mark.parametrize("operation", ["clear", "replace"])
async def test_old_import_retry_never_resurrects_retired_goal(goal_case, operation):
    workspace, _, store, goal = goal_case
    path = _legacy(workspace, goal)
    payload = path.read_bytes()
    assert import_legacy_goals(store) == 1
    if operation == "clear":
        assert store.goal_clear(goal.session_id)
        expected = None
    else:
        expected = goal.model_copy(update={"goal_id": "goal_new", "objective": "独立的新目标", "revision": 0})
        store.goal_write(expected)
    path.write_bytes(payload)
    assert import_legacy_goals(store) == 1
    assert store.goal_read(goal.session_id) == expected


async def test_import_rejects_conflict_without_deleting_original(goal_case):
    workspace, state, store, goal = goal_case
    path = _legacy(workspace, goal)
    store.goal_write(goal.model_copy(update={"objective": "统一 owner 已有不同事实"}))
    with pytest.raises(RuntimeError, match="owner 冲突"):
        import_legacy_goals(store)
    assert path.exists()
    with closing(state.connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persistent_task_imports").fetchone()[0] == 0


async def test_import_fingerprint_conflict_is_explicit(goal_case):
    workspace, _, store, goal = goal_case
    _legacy(workspace, goal)
    assert import_legacy_goals(store) == 1
    path = _legacy(workspace, goal.model_copy(update={"objective": "外部篡改旧输入"}))
    with pytest.raises(RuntimeError, match="指纹冲突"):
        import_legacy_goals(store)
    assert path.exists() and store.goal_read(goal.session_id) == goal


async def test_import_and_receipt_roll_back_in_one_transaction(goal_case):
    workspace, state, store, goal = goal_case
    path = _legacy(workspace, goal)
    with closing(state.connection()) as connection:
        connection.execute("CREATE TRIGGER reject_import BEFORE INSERT ON persistent_task_imports BEGIN SELECT RAISE(ABORT,'import-fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match="import-fault"):
        import_legacy_goals(store)
    assert path.exists() and store.goal_read(goal.session_id) is None
    with closing(state.connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persistent_tasks").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_import")
    assert import_legacy_goals(store) == 1 and store.goal_read(goal.session_id) == goal


async def test_preparing_goal_is_hidden_and_abort_uses_exact_journal(goal_case):
    workspace, _, store, goal = goal_case
    record = await workspace.creation_service.prepare(
        idempotency_key="goal-fork", title="准备中的 fork", title_source="auto", purpose="context_fork",
        parent_node_id=None, graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata=make_session_metadata(kind="context_fork", context_source_session_id=goal.session_id),
        request_intent={"goal_snapshot": {"goal_id": "goal_fork", "objective": "冻结目标", "token_budget": None}},
    )
    fork_goal = goal.model_copy(update={"goal_id": "goal_fork", "session_id": record.session_id,
                                       "thread_id": record.main_thread_id, "objective": "冻结目标"})
    directory = workspace.resolver.resolve_prepared_session_node(record)
    (directory / "goal.json").write_text(fork_goal.model_dump_json(exclude={"thread_id"}), encoding="utf-8")
    assert import_legacy_goals(store) == 1
    assert store.goal_read(record.session_id, creation=record) == fork_goal
    assert store.goal_list() == []
    store.goal_abort_fork(record)
    assert store.goal_read(record.session_id, creation=record) is None


async def test_goal_missing_definition_never_becomes_an_empty_goal(goal_case):
    _, state, store, goal = goal_case
    store.goal_write(goal)
    with closing(state.connection()) as connection, connection:
        connection.execute("DELETE FROM persistent_task_definitions WHERE task_id=?", (goal.goal_id,))
    with pytest.raises(RuntimeError, match="缺少不可变定义"):
        store.goal_read(goal.session_id)
