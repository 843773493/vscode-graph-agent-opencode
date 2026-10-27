"""view 创建顺序的显式25→26升级及原子回滚。"""

from __future__ import annotations

import pytest

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    VIEW_CREATION_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema25,
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,  # noqa: PLC0414 - 使用真实准入、封存和checkpoint
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as epoch_db,  # noqa: PLC0414 - 使用当前测试文件的隔离工作区
)


@pytest.mark.parametrize("interrupted", [False, True])
def test_schema25_view_order_upgrade_preserves_history_or_rolls_back(epoch_case, monkeypatch, interrupted):
    case = epoch_case
    case.load_snapshot()
    assembly = case.snapshot(case.prepare("before-view-order-migration"))
    case.checkpoint("view-order-checkpoint")
    original_assembly = canonical_json_bytes(assembly.to_dict())
    storage = case.saver._storage.storage
    jsonl = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_jsonl = jsonl.read_bytes()
    restore_schema25(case.connection)
    original_views = case.connection.execute("SELECT rowid,* FROM context_views ORDER BY rowid").fetchall()
    expected_order = [(row[1], row[0]) for row in original_views]
    original_migrate = rollout_maintenance_owner.execute_atomic_schema_sql

    if interrupted:
        def fail_after_ddl(connection, script):
            original_migrate(connection, script)
            raise RuntimeError("view创建顺序迁移发布前中断")

        monkeypatch.setattr(rollout_maintenance_owner, "execute_atomic_schema_sql", fail_after_ddl)
        with pytest.raises(RuntimeError, match="发布前中断"):
            storage.migrate_schema(
                case.owner.thread_id, session_id=case.owner.session_id, to_version=26,
                migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
            )
        assert case.connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (25, "recovery_required")
        assert case.connection.execute("SELECT rowid,* FROM context_views ORDER BY rowid").fetchall() == original_views
        assert case.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'context_views_%creation_sequence'",
        ).fetchall() == []
    else:
        storage.migrate_schema(
            case.owner.thread_id, session_id=case.owner.session_id, to_version=26,
            migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
        )
        assert case.connection.execute(
            "SELECT view_id,creation_sequence FROM context_views ORDER BY creation_sequence",
        ).fetchall() == expected_order
        case.restart()
        assert canonical_json_bytes(case.snapshot({"assembly_id": assembly.assembly_id}).to_dict()) == original_assembly
    assert jsonl.read_bytes() == original_jsonl
