"""真实 Session 的 catalog 和备份必须拒绝 quick_check 漏检的损坏。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_catalog_store.queries import _register_session_list_functions
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


def _corrupt_catalog(path: Path, session_id: str, corruption: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        if corruption == "index":
            # 只改索引声明而不重建 btree，模拟外部改写；quick_check 不检查索引内容。
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=replace(sql, 'nodes(parent_node_id)', "
                "'nodes(display_name)') WHERE name='idx_nodes_parent'",
            )
        else:
            # 外来连接未启用外键，可写入不存在的父节点；产品不得在重启时吸收它。
            connection.execute(
                "UPDATE nodes SET parent_node_id=? WHERE node_id=?",
                (create_prefixed_id("ses"), session_id),
            )
        connection.commit()
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        if corruption == "index":
            assert connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        else:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("corruption", ["index", "foreign_key"])
async def test_startup_rejects_sqlite_corruption_without_repair(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, corruption: str,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="启动完整性核验"))
    path, files = _close_and_files(container)
    _corrupt_catalog(path, session.session_id, corruption)
    before = path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="完整性校验失败"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert path.read_bytes() == before
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("corruption", ["index", "foreign_key"])
async def test_matching_backup_checksum_cannot_hide_sqlite_corruption(
    startup_container: AppContainer, corruption: str,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="备份完整性核验"))
    store = container.session_service.path_resolver.catalog_store
    path = container.workspace_root / "catalog-backup.sqlite"
    manifest = store.create_consistent_backup(path)
    _corrupt_catalog(path, session.session_id, corruption)
    before = path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="完整性校验失败"):
        store.verify_consistent_backup(
            path, expected_checksum=hashlib.sha256(before).hexdigest(),
            backup_generation=manifest.generation,
        )
    assert path.read_bytes() == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    assert (await container.session_service.get(session.session_id)).title == session.title


async def test_healthy_catalog_reopens_and_verifies_expression_index_backup(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="完整性健康对照"))
    store = container.session_service.path_resolver.catalog_store
    path = container.workspace_root / "healthy-backup.sqlite"
    manifest = store.create_consistent_backup(path)
    before = path.read_bytes()
    report = store.verify_consistent_backup(
        path, expected_checksum=manifest.checksum, backup_generation=manifest.generation,
    )
    assert report.quick_check == "ok"
    assert report.generation == manifest.generation
    assert path.read_bytes() == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    catalog_path, files = _close_and_files(container)
    catalog_before = catalog_path.read_bytes()
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert (await restarted.session_service.get(session.session_id)).title == session.title
    assert catalog_path.read_bytes() == catalog_before
    _assert_body_unchanged(container, files)
