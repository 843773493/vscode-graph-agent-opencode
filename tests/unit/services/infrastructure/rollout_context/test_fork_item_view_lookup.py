"""只读 item/view 定位合同；执行与捕获的完整验收由 fork 集成测试负责。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    CheckpointForkSource,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    replace_members,
)


@pytest.fixture
def item_views(source_seal_case):
    """用真实空库及 acceptance item 准备查询专用分支，不伪造执行验收。"""
    case = source_seal_case
    owner = case.owner
    storage = case.saver._storage.storage
    item_id = str(case.accepted["root_input_item_id"])

    @contextmanager
    def connection():
        with storage._connect(owner.thread_id, session_id=owner.session_id) as database:
            yield database

    with connection() as database:
        root_branch = database.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns=''",
        ).fetchone()[0]
        for branch, parent in (("old-branch", root_branch), ("latest-branch", root_branch), ("foreign-branch", None)):
            database.execute(
                "INSERT INTO branches(branch_id,branch_kind,status,parent_branch_id,created_at,updated_at) "
                "VALUES (?,'rewind','inactive',?,'2000-01-01','2000-01-01')", (branch, parent),
            )
        database.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns,active_branch_id,created_at,updated_at) "
            "VALUES ('foreign','foreign-branch','2000-01-01','2000-01-01')",
        )
        for view, branch, kind, sequence, visible, selected in (
            ("old", "old-branch", "checkpoint", 50, 1, 1),
            ("latest", "latest-branch", "checkpoint", 80, 1, 1),
            ("hidden", "latest-branch", "checkpoint", 90, 0, 1),
            ("unselected", "latest-branch", "checkpoint", 91, 1, 0),
            ("backing", "latest-branch", "checkpoint_messages", 100, 1, 1),
            ("foreign", "foreign-branch", "checkpoint", 110, 1, 1),
        ):
            database.execute(
                "INSERT INTO context_views(view_id,branch_id,view_kind,head_message_sequence,"
                "logical_turn_count,control_sequence,created_at) VALUES (?,?,?,0,0,?,'2000-01-01')",
                (view, branch, kind, sequence),
            )
            replace_members(database, view, [(item_id, 0, visible, "canonical", selected, 0 if selected else None)])
        database.commit()
    return connection, item_id


def test_item_lookup_uses_all_namespace_branches_and_publication_order(item_views):
    connection, item_id = item_views
    with connection() as database:
        # 较晚的 wall clock、尾部写入或另一 namespace 都不使旧 view 成为最新候选。
        database.execute("UPDATE context_views SET created_at='2099-01-01',head_message_sequence=999 WHERE view_id='old'")
        assert CheckpointForkSource._locate_item_view(
            database, checkpoint_ns="", item_id=item_id, view_id=None,
        ) == "latest"
        assert CheckpointForkSource._locate_item_view(
            database, checkpoint_ns="foreign", item_id=item_id, view_id=None,
        ) == "foreign"


def test_explicit_item_view_does_not_follow_latest(item_views):
    connection, item_id = item_views
    with connection() as database:
        assert CheckpointForkSource._locate_item_view(
            database, checkpoint_ns="", item_id=item_id, view_id="old",
        ) == "old"


@pytest.mark.parametrize("view", ["hidden", "unselected", "backing", "foreign", "missing"])
def test_explicit_ineligible_view_is_rejected_without_fallback(item_views, view):
    connection, item_id = item_views
    with connection() as database, pytest.raises(ValueError, match="source-mismatch"):
        CheckpointForkSource._locate_item_view(database, checkpoint_ns="", item_id=item_id, view_id=view)


def test_missing_item_never_uses_other_visible_members(item_views):
    connection, _ = item_views
    with connection() as database, pytest.raises(ValueError, match="source-mismatch"):
        CheckpointForkSource._locate_item_view(database, checkpoint_ns="", item_id="missing", view_id=None)


def test_active_successor_uses_its_original_publication_not_append_time(item_views):
    connection, item_id = item_views
    with connection() as database:
        database.execute(
            "INSERT INTO context_views(view_id,branch_id,parent_view_id,view_kind,head_message_sequence,"
            "logical_turn_count,control_sequence,created_at) "
            "VALUES ('successor','latest-branch','latest','active',0,0,80,'2000-01-01')",
        )
        replace_members(database, "successor", [(item_id, 0, 1, "canonical", 1, 0)])
        assert CheckpointForkSource._locate_item_view(
            database, checkpoint_ns="", item_id=item_id, view_id=None,
        ) == "successor"


def test_old_view_control_event_does_not_change_creation_order(item_views):
    connection, item_id = item_views
    with connection() as database:
        database.execute("UPDATE context_views SET control_sequence=900 WHERE view_id='old'")
        assert CheckpointForkSource._locate_item_view(
            database, checkpoint_ns="", item_id=item_id, view_id=None,
        ) == "latest"


def test_creation_order_is_immutable_and_unique(item_views):
    connection, _ = item_views
    with connection() as database, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.execute("UPDATE context_views SET creation_sequence=900 WHERE view_id='old'")
    with connection() as database, pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        database.execute(
            "INSERT INTO context_views(view_id,branch_id,view_kind,head_message_sequence,logical_turn_count,"
            "created_at,creation_sequence) SELECT 'collision',branch_id,'checkpoint',0,0,'2000-01-01',"
            "creation_sequence FROM context_views WHERE view_id='old'",
        )
