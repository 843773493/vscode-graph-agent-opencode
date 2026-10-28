"""Gateway 每个既有迁移版本升级和冷重开保持结构与业务行。"""

import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS, GatewayStateStore
from app.services.infrastructure.config.state import ConfigConflictError


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
    assert [row[0] for row in snapshot["versions"]] == list(range(1, 19))
    with GatewayStateStore(path=path) as reopened, closing(reopened.connection()) as connection:
        assert _snapshot(connection) == snapshot
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT native_root FROM native_directory_roots").fetchall() == []


def test_gateway_pending_location_migration_preserves_candidate_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "gateway.sqlite"

    def create(store: GatewayStateStore, *, digest: str = "candidate-digest"):
        return store.create_pending_config_candidate(
            config_domain="gateway", candidate_id="location-candidate",
            idempotency_key="location-replay", payload={"token": "env:TEST_KEY"},
            source_baseline={"user": {"revision": 7, "digest": "source-digest"}},
            candidate_digest=digest, effective_digest="effective-digest",
            target_generation="generation-new", fencing_token="fence-original",
            state="candidate_validated", base_active_revision=5, source_generation=11,
        )

    # facade 持有同一迁移元组；旧库必须由原有构造入口创建。
    with monkeypatch.context() as legacy:
        legacy.setattr("app.gateway.control.gateway_state._GATEWAY_MIGRATIONS", _GATEWAY_MIGRATIONS[:17])
        with GatewayStateStore(path=path) as old:
            candidate = create(old)
            with closing(old.connection()) as connection:
                connection.execute("UPDATE config_pending_candidate SET persistence_location = ?", (str(path),))
                original = dict(connection.execute("SELECT * FROM config_pending_candidate").fetchone())
                del original["persistence_location"]
                versions = list(connection.execute("SELECT * FROM schema_migrations ORDER BY version"))

    for _ in range(2):
        with GatewayStateStore(path=path) as store:
            with closing(store.connection()) as connection:
                persisted = dict(connection.execute("SELECT * FROM config_pending_candidate").fetchone())
                assert persisted == original
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                assert list(connection.execute("SELECT * FROM schema_migrations ORDER BY version"))[:17] == versions
            latest = store.get_pending_config_candidate(config_domain="gateway")
            assert latest is not None
            assert "persistence_location" not in asdict(latest)
            assert store.get_pending_config_candidate(config_domain="gateway", candidate_id=candidate.candidate_id) == candidate
            assert store.get_pending_config_candidate(config_domain="gateway", idempotency_key=candidate.idempotency_key) == candidate
            assert create(store) == candidate
            with pytest.raises(ConfigConflictError, match="idempotency"):
                create(store, digest="conflicting-digest")
