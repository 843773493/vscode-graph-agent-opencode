"""共享 source owner 的旧列迁移与事务失败语义。"""

import sqlite3
from pathlib import Path

import pytest

from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner


@pytest.fixture
def legacy_source_database(tmp_path: Path) -> Path:
    path = tmp_path / "source-owner.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE config_source_journal (
                source_key TEXT NOT NULL, source_generation INTEGER NOT NULL,
                source_event_id TEXT NOT NULL UNIQUE, source_path TEXT,
                presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
                layer_revision INTEGER NOT NULL, layer_digest TEXT,
                previous_digest TEXT, origin TEXT NOT NULL,
                fanout_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (source_key, source_generation)
            );
            CREATE INDEX config_source_journal_path_idx
                ON config_source_journal(source_path);
            CREATE TABLE config_source_fanout (
                source_key TEXT NOT NULL, source_generation INTEGER NOT NULL,
                workspace_id TEXT NOT NULL, status TEXT NOT NULL,
                layer_revision INTEGER, layer_digest TEXT, result TEXT, error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_key, source_generation, workspace_id),
                FOREIGN KEY (source_key, source_generation)
                    REFERENCES config_source_journal(source_key, source_generation)
                    ON DELETE CASCADE
            );
            INSERT INTO config_source_journal VALUES (
                'workspace:user', 7, 'event-7', '/legacy/private/workspace.jsonc',
                'present', 9, 'digest-7', 'digest-6', 'watcher', 'fanout-7',
                '2026-10-21T00:00:00+00:00'
            );
            INSERT INTO config_source_fanout VALUES (
                'workspace:user', 7, 'workspace-a', 'applied', 9, 'digest-7',
                'applied', NULL, '2026-10-21T00:00:00+00:00'
            );
        """)
    return path


def test_source_uri_migration_preserves_journal_and_fanout_on_cold_reopen(
    legacy_source_database: Path,
) -> None:
    for _ in range(2):
        owner = WorkspaceSourceOwner(path=legacy_source_database)
        assert owner.get(source_key="workspace:user", source_generation=7).uri is None
        assert owner.get_fanout(
            source_key="workspace:user", source_generation=7, workspace_id="workspace-a",
        ).status == "applied"
        owner.close()
        with sqlite3.connect(legacy_source_database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(config_source_journal)")}
            assert "uri" in columns and "source_path" not in columns
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT * FROM config_source_journal").fetchall() == [(
                "workspace:user", 7, "event-7", None, "present", 9, "digest-7",
                "digest-6", "watcher", "fanout-7", "2026-10-21T00:00:00+00:00",
            )]


def test_source_uri_migration_failure_rolls_back_schema_and_data(
    legacy_source_database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = WorkspaceSourceOwner._connect

    def connect_with_failed_drop(owner):
        connection = connect(owner)
        connection.set_authorizer(lambda action, table, *_: (
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE
            and table == "config_source_journal" else sqlite3.SQLITE_OK
        ))
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceSourceOwner, "_connect", connect_with_failed_drop)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            WorkspaceSourceOwner(path=legacy_source_database)

    with sqlite3.connect(legacy_source_database) as connection:
        assert connection.execute("SELECT source_path FROM config_source_journal").fetchall() == [
            ("/legacy/private/workspace.jsonc",),
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'config_source_journal__new'",
        ).fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM config_source_fanout").fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'config_source_journal_path_idx'",
        ).fetchone() == ("config_source_journal_path_idx",)
    owner = WorkspaceSourceOwner(path=legacy_source_database)
    assert owner.get(source_key="workspace:user", source_generation=7).layer_digest == "digest-7"
    owner.close()
