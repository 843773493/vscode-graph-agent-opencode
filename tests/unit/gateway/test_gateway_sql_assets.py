"""Gateway 每个既有迁移版本升级和冷重开保持结构与业务行。"""

import sqlite3
from contextlib import closing

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS, GatewayStateStore


def _snapshot(connection):
    return {
        "schema": [tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )],
        "versions": [tuple(row) for row in connection.execute(
            "SELECT version,applied_at FROM schema_migrations ORDER BY version"
        )],
        "config": [tuple(row) for row in connection.execute("SELECT * FROM gateway_config")],
    }


@pytest.mark.parametrize("version", range(1, len(_GATEWAY_MIGRATIONS) + 1))
def test_gateway_upgrade_from_each_existing_version_and_reopen(tmp_path, version):
    path = tmp_path / "gateway.sqlite"
    with (
        SQLiteStateDatabase(path=path, schema_version=version,
                            migrations=_GATEWAY_MIGRATIONS[:version]) as old,
        closing(old.connection()) as connection,
    ):
        connection.execute(
            "INSERT INTO gateway_config VALUES ('control-marker', 9, '{}', 'frozen')"
        )
        original_versions = list(connection.execute("SELECT * FROM schema_migrations"))
    with GatewayStateStore(path=path) as state, closing(state.connection()) as connection:
        snapshot = _snapshot(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert snapshot["config"] == [("control-marker", 9, "{}", "frozen")]
    assert snapshot["versions"][:version] == [tuple(row) for row in original_versions]
    assert [row[0] for row in snapshot["versions"]] == list(range(1, 18))
    with GatewayStateStore(path=path) as reopened, closing(reopened.connection()) as connection:
        assert _snapshot(connection) == snapshot
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT native_root FROM native_directory_roots").fetchall() == []
