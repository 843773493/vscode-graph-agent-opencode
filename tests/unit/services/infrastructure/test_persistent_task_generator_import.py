"""旧生成器的原身份、历史与计划游标一次性进入同一 SQLite 事务。"""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.abstractions.state_conflict import ClientStateConflictError
from app.schemas.internal_v2.generator_import import SessionGeneratorImportRequest
from app.schemas.internal_v2.session_navigation.generation_state import (
    GenerationHistoricalState,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationExecuteResultDTO,
    SessionGenerationOutputDTO,
)
from app.services.infrastructure.persistent_tasks.generator_import import (
    ImportedGenerationRun,
    imported_files,
    install_generator,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


@pytest.fixture
def imported_case(empty_workspace_scope):
    now = datetime(2026, 10, 27, tzinfo=UTC)
    request = SessionGeneratorImportRequest.model_validate({
        "task_id": "gen_old", "source_hash": "a" * 64, "revision": 9, "lifecycle": "active", "workspace_ids": {"route": "ws_owner"},
        "created_at": now, "updated_at": now, "next_run_at": now + timedelta(seconds=60), "runs": [],
        "configuration": {"name": "原任务", "trigger": {"type_id": "builtin.interval", "version": "1", "interval_seconds": 60},
            "condition": {"type_id": "builtin.always", "version": "1"}, "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
            "action": {"type_id": "builtin.session_generation", "version": "1", "generation": {
                "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"}, "config": {"prompt": "原提示"},
                "placement": {"kind": "workspace", "workspace_id": "ws_owner"}, "context_source": {"kind": "fresh"},
                "session_strategy": {"mode": "new_per_run"},
            }},
        },
    })
    with WorkspaceStateStore(workspace_root=empty_workspace_scope[1]) as state:
        yield PersistentTaskStore(state, workspace_id="ws_owner"), state, request, now


def test_import_keeps_terminal_output_after_session_deletion_and_advances_accepted_prefix(imported_case):
    store, state, request, now = imported_case
    result = SessionGenerationExecuteResultDTO(run_id="grun_original", status="completed", job_id="job_original", message_id="msg_original",
        outputs=[SessionGenerationOutputDTO(workspace_id="ws_owner", session_id="ses_deleted", title="旧标题", navigation_path=["旧目录"])])
    old = ImportedGenerationRun(run_id=result.run_id, trigger_key="old-scheduler-key", trigger_type="interval", scheduled_for=request.next_run_at,
        created_at=now, updated_at=now, phase="delivered", state=GenerationHistoricalState(value=result), configuration=request.configuration, error=None)
    install_generator(store, request, [old], {".boxteam/generation-runs/input.json": "b" * 64}, reason=None)
    task = store.get(request.task_id)
    assert task.revision == 9 and task.next_run_at == now + timedelta(seconds=120)
    run, = store.runs(task.task_id)
    assert run.run_id == result.run_id and run.trigger_key == "old-scheduler-key" and run.generation == result
    assert store.runs(task.task_id, pending_only=True) == []
    assert imported_files(store, request) == {".boxteam/generation-runs/input.json": "b" * 64}
    changed = request.model_copy(update={"source_hash": "c" * 64})
    with pytest.raises(ClientStateConflictError, match="源快照发生变化"):
        imported_files(store, changed)
    with closing(state.connection()) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_rolls_back_definition_run_schedule_and_receipt_together(imported_case):
    store, state, request, _now = imported_case
    with closing(state.connection()) as connection:
        connection.execute("CREATE TRIGGER reject_import BEFORE INSERT ON persistent_task_generator_imports BEGIN SELECT RAISE(ABORT,'receipt-fault'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="receipt-fault"):
        install_generator(store, request, [], {}, reason=None)
    assert store.list() == [] and imported_files(store, request) is None
    with closing(state.connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persistent_task_definitions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM persistent_task_schedules").fetchone()[0] == 0
