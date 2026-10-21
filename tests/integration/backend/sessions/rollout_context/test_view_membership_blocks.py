"""成员块结构、事务和旧库迁移；产品fork/恢复另由真实入口负载验收。"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    append_members,
    clone_members,
    read_members,
    replace_members,
    store_block,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    VIEW_MEMBERSHIP_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema22,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 使用当前测试目录工作区
)


@pytest.fixture
def member_case(registry_db):
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    with connection:
        for view in ("shared-members-source", "shared-members-target"):
            connection.execute(
                "INSERT INTO context_views(view_id,branch_id,parent_view_id,view_kind,head_turn_id,"
                "head_message_sequence,logical_turn_count,created_at) "
                "SELECT ?,branch_id,NULL,'checkpoint',head_turn_id,head_message_sequence,logical_turn_count,created_at "
                "FROM context_views LIMIT 1", (view,),
            )
        members = [(f"member-{i}", i, 1, "canonical", 1, i) for i in range(48)]
        replace_members(connection, "shared-members-source", members)
    return saver, session, thread, connection, members


def test_clone_and_append_share_frozen_blocks_and_keep_selection_independent(member_case):
    _saver, _session, _thread, connection, members = member_case
    original_count = connection.execute("SELECT count(*) FROM context_membership_items").fetchone()[0]
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        clone_members(connection, "shared-members-source", "shared-members-target")
    assert connection.execute("SELECT count(*) FROM context_membership_items").fetchone()[0] == original_count
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        append_members(connection, "shared-members-target", [("appended", 48, 1, "display_only", 1, 48)])
    assert read_members(connection, "shared-members-source") == members
    assert connection.execute("SELECT count(*) FROM context_membership_items").fetchone()[0] == original_count + 1
    changed = list(members)
    changed[5] = (*changed[5][:2], 0, "canonical", 0, None)
    changed[6] = (*changed[6][:4], 1, 9)
    changed[9] = (*changed[9][:4], 1, 6)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        replace_members(connection, "shared-members-target", changed)
    assert read_members(connection, "shared-members-source") == members
    assert read_members(connection, "shared-members-target") == changed
    assert connection.execute("SELECT type FROM sqlite_master WHERE name='context_view_items'").fetchone() == ("view",)


def test_duplicate_coordinates_and_content_collision_fail_without_publishing(member_case):
    _saver, _session, _thread, connection, members = member_case
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        clone_members(connection, "shared-members-source", "shared-members-target")
    before = read_members(connection, "shared-members-target")
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("BEGIN IMMEDIATE")
        replace_members(connection, "shared-members-target", [*members, ("other", 0, 1, "canonical", 1, 49)])
    assert read_members(connection, "shared-members-target") == before
    with connection:
        connection.execute("UPDATE context_membership_items SET visible=0 WHERE item_id='member-0'")
    with pytest.raises(ValueError, match="同版本异内容"), connection:
        connection.execute("BEGIN IMMEDIATE")
        store_block(connection, members[:16])


@pytest.mark.parametrize("interrupted", [False, True])
def test_explicit_schema22_migration_preserves_all_coordinates_or_rolls_back(member_case, monkeypatch, interrupted):
    saver, session, thread, connection, _members = member_case
    restore_schema22(connection)
    before = connection.execute("SELECT * FROM context_view_items ORDER BY view_id,logical_item_ordinal").fetchall()
    if interrupted:
        migrate = rollout_maintenance_owner.migrate_members

        def fail_after_conversion(db):
            migrate(db)
            raise RuntimeError("view迁移发布前中断")

        monkeypatch.setattr(rollout_maintenance_owner, "migrate_members", fail_after_conversion)
        with pytest.raises(RuntimeError, match="发布前中断"):
            saver._storage.storage.migrate_schema(thread, session_id=session, to_version=23,
                                                 migration_name="view_membership", migration_sql=VIEW_MEMBERSHIP_MIGRATION_SQL)
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (22, "recovery_required")
        assert connection.execute("SELECT type FROM sqlite_master WHERE name='context_view_items'").fetchone() == ("table",)
    else:
        saver._storage.storage.migrate_schema(thread, session_id=session, to_version=23,
                                             migration_name="view_membership", migration_sql=VIEW_MEMBERSHIP_MIGRATION_SQL)
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (23, "active")
        assert connection.execute("SELECT type FROM sqlite_master WHERE name='context_view_items'").fetchone() == ("view",)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='context_view_items_legacy'").fetchone() is None
    assert connection.execute("SELECT * FROM context_view_items ORDER BY view_id,logical_item_ordinal").fetchall() == before
