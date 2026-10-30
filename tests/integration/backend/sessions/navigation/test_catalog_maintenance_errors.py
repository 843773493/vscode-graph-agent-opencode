"""实际 Session 和导航事实损坏后，维护拒绝保全原件及可恢复的身份。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_catalog_store.queries import _register_session_list_functions
from tests.integration.backend.sessions.test_catalog_navigation_schema import (
    _create_navigation_facts,
)
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

_PRIVATE = "/private/catalog-maintenance-column"


@pytest.mark.parametrize("entry", ["startup", "backup"])
@pytest.mark.parametrize("fault", ["authority-column", "queue-index", "foreign-key"])
async def test_maintenance_failure_keeps_product_history_and_explicit_backup_restore(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture, entry: str, fault: str,
) -> None:
    container = startup_container
    await _create_navigation_facts(container)
    session, = (await container.session_service.list()).items
    store = container.session_service.path_resolver.catalog_store
    revision = container.session_catalog_service.path_resolver.revision
    snapshot = container.workspace_root / "catalog-original.sqlite"
    manifest = store.create_consistent_backup(snapshot)
    healthy = snapshot.read_bytes()
    if entry == "startup":
        path, files = _close_and_files(container)
    else:
        path = snapshot
        root = container.workspace_root / ".boxteam"
        files = {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
                 for p in (root / directory).rglob("*") if p.is_file()}
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        if fault == "authority-column":
            connection.execute(f'ALTER TABLE nodes RENAME COLUMN title_source TO "{_PRIVATE}"')
        elif fault == "queue-index":
            connection.execute("DROP INDEX idx_navigation_mutation_queue")
        else:
            connection.execute("UPDATE nodes SET parent_node_id='missing-parent' WHERE node_id=?", (session.session_id,))
        connection.commit()
    before = path.read_bytes()
    caplog.set_level(logging.ERROR)
    with pytest.raises(CatalogMaintenanceRequiredError) as caught:
        if entry == "startup":
            _build_owned_container(container.workspace_root, monkeypatch, request)
        else:
            store.verify_consistent_backup(
                path, expected_checksum=hashlib.sha256(before).hexdigest(), backup_generation=manifest.generation,
            )
    public = str(caught.value)
    assert str(path) not in public and str(container.workspace_root) not in public and _PRIVATE not in public
    assert "artifact=session_catalog" in public and "stage=catalog-" in public
    assert str(path) in caplog.text
    if fault == "authority-column":
        assert _PRIVATE in caplog.text
    assert caught.value.__cause__ is not None
    assert path.read_bytes() == before
    _assert_body_unchanged(container, files)
    if entry == "backup":
        assert not snapshot.with_name(snapshot.name + "-wal").exists()
        assert not snapshot.with_name(snapshot.name + "-shm").exists()
        assert (await container.session_service.get(session.session_id)) == session
        assert store.current_generation() == manifest.generation
    else:
        # 模拟显式恢复产品生成的已核对备份；构造器本身绝不修复或覆盖损坏库。
        path.write_bytes(healthy)
        reopened = _build_owned_container(container.workspace_root, monkeypatch, request)
        assert (await reopened.session_service.get(session.session_id)) == session
        assert reopened.session_catalog_service.path_resolver.revision == revision
        _assert_body_unchanged(reopened, files)
