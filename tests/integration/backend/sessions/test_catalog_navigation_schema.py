"""真实导航操作生成的队列事实缺失时，不得在冷启动中补建空表。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)

_QUEUE_TABLES = (
    "navigation_mutation_records", "navigation_events", "navigation_queue_owners",
)
_CORRUPTIONS = (
    *_QUEUE_TABLES, "all_queue_tables",
    "idx_navigation_mutation_queue", "idx_navigation_mutation_pending",
    "index_definition", "table_definition",
)


async def _create_navigation_facts(container: AppContainer) -> None:
    await container.session_service.create(SessionCreateRequest(title="保留会话与目录事实"))
    catalog = container.session_catalog_service
    receipt = await submit_catalog_intent(
        catalog, create_catalog_intent(catalog, "create_folder", name="真实导航操作"),
    )
    assert receipt.state == "committed" and receipt.result_node_id is not None
    with catalog.path_resolver.catalog_store.read_transaction() as connection:
        assert all(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in _QUEUE_TABLES)


def _corrupt_navigation_schema(path: Path, corruption: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        if corruption in _QUEUE_TABLES or corruption == "all_queue_tables":
            for name in _QUEUE_TABLES if corruption == "all_queue_tables" else (corruption,):
                connection.execute(f"DROP TABLE {name}")
        elif corruption.startswith("idx_"):
            connection.execute(f"DROP INDEX {corruption}")
        elif corruption == "index_definition":
            connection.execute("DROP INDEX idx_navigation_mutation_queue")
            connection.execute(
                "CREATE INDEX idx_navigation_mutation_queue ON navigation_mutation_records(workspace_id, queue_seq)",
            )
        else:
            connection.execute("ALTER TABLE navigation_events RENAME COLUMN error_detail TO external_detail")
        connection.commit()


@pytest.mark.parametrize("corruption", _CORRUPTIONS)
async def test_startup_rejects_missing_or_redefined_navigation_schema(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, corruption: str,
) -> None:
    container = startup_container
    await _create_navigation_facts(container)
    path, files = _close_and_files(container)
    _corrupt_navigation_schema(path, corruption)
    before = path.read_bytes()
    # 不只检查core Store：旧实现会在业务queue构造时静默补表，必须穿过真实容器入口。
    with pytest.raises(RuntimeError, match="(缺表|navigation.*schema)"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert path.read_bytes() == before
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("corruption", _CORRUPTIONS)
async def test_backup_rejects_missing_or_redefined_navigation_schema(
    startup_container: AppContainer, corruption: str,
) -> None:
    container = startup_container
    await _create_navigation_facts(container)
    store = container.session_catalog_service.path_resolver.catalog_store
    path = container.workspace_root / "navigation-schema-backup.sqlite"
    manifest = store.create_consistent_backup(path)
    _corrupt_navigation_schema(path, corruption)
    before = path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="备份.*(缺表|navigation.*schema)"):
        store.verify_consistent_backup(
            path, expected_checksum=hashlib.sha256(before).hexdigest(),
            backup_generation=manifest.generation,
        )
    assert path.read_bytes() == before
    assert store.current_generation() == manifest.generation
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


async def test_current_navigation_schema_reopens_without_rewriting_records(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    await _create_navigation_facts(container)
    store = container.session_catalog_service.path_resolver.catalog_store
    backup = container.workspace_root / "current-navigation-backup.sqlite"
    manifest = store.create_consistent_backup(backup)
    backup_bytes = backup.read_bytes()
    report = store.verify_consistent_backup(
        backup, expected_checksum=manifest.checksum, backup_generation=manifest.generation,
    )
    assert report.generation == manifest.generation and report.quick_check == "ok"
    assert backup.read_bytes() == backup_bytes
    path, files = _close_and_files(container)
    before = path.read_bytes()
    with closing(sqlite3.connect(path)) as connection:
        frozen = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in _QUEUE_TABLES}
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    with restarted.session_catalog_service.path_resolver.catalog_store.read_transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema_version
        for name, rows in frozen.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] == rows
    assert path.read_bytes() == before
    _assert_body_unchanged(container, files)
