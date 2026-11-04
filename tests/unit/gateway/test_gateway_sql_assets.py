"""Gateway 每个既有迁移版本升级和冷重开保持结构与业务行。"""

import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS, GatewayStateStore
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    build_secret_binding_summary,
    dump_json,
)


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
    assert [row[0] for row in snapshot["versions"]] == list(range(1, 21))
    with GatewayStateStore(path=path) as reopened, closing(reopened.connection()) as connection:
        assert _snapshot(connection) == snapshot
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT native_root FROM native_directory_roots").fetchall() == []


def test_gateway_pending_location_migration_preserves_candidate_and_replay(
    tmp_path: Path,
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

    # 旧版本原件按旧 DDL 写入；新 API 不在缺少新列的伪旧 schema 上执行。
    with (
        SQLiteStateDatabase(path=path, schema_version=17, migrations=_GATEWAY_MIGRATIONS[:17]) as old,
        closing(old.connection()) as connection,
    ):
        connection.execute("INSERT INTO config_revision_meta VALUES ('gateway', 2)")
        connection.execute(
            "INSERT INTO config_pending_candidate("
            "config_domain,candidate_id,idempotency_key,pending_revision,payload_json,source_baseline_json,"
            "candidate_digest,effective_digest,target_generation,fencing_token,state,created_at,"
            "base_active_revision,source_generation,secret_bindings_json,persistence_location) "
            "VALUES ('gateway','location-candidate','location-replay',1,?,?,'candidate-digest','effective-digest',"
            "'generation-new','fence-original','candidate_validated','2026-01-01T00:00:00+00:00',5,11,?,?)",
            (dump_json({"token": "env:TEST_KEY"}), dump_json({"user": {"revision": 7, "digest": "source-digest"}}),
             dump_json(build_secret_binding_summary({"token": "env:TEST_KEY"})), str(path)),
        )
        original = dict(connection.execute("SELECT * FROM config_pending_candidate").fetchone())
        del original["persistence_location"]
        original["source_edit_json"] = None
        versions = list(connection.execute("SELECT * FROM schema_migrations ORDER BY version"))

    candidate = None
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
            if candidate is None:
                candidate = latest
            assert latest == candidate and latest.source_edit is None
            assert "persistence_location" not in asdict(latest)
            assert store.get_pending_config_candidate(config_domain="gateway", candidate_id=candidate.candidate_id) == candidate
            assert store.get_pending_config_candidate(config_domain="gateway", idempotency_key=candidate.idempotency_key) == candidate
            assert create(store) == candidate
            with pytest.raises(ConfigConflictError, match="idempotency"):
                create(store, digest="conflicting-digest")
