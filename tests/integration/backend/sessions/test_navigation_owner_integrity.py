"""导航worker接管与fencing不得覆盖或截断损坏的durable owner身份。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.sqlite_state import utc_now_text
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationQueueOwnerError,
)
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_navigation_event_integrity import (
    _create_event,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


def _rewrite_owner(path: Path, column: str, value: object) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE navigation_queue_owners SET {column}=?", (value,))
        connection.commit()


@pytest.mark.parametrize("column,value", [
    ("owner_generation", 0),
    ("owner_generation", -1),
    ("owner_generation", 1.5),
    ("owner_generation", "invalid-generation"),
    ("owner_id", ""),
    ("workspace_id", "another-workspace"),
    ("updated_at", "invalid-time"),
    ("updated_at", "2026-10-23T00:00:00"),
])
async def test_startup_rejects_corrupt_navigation_owner_without_replacing_generation(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, column: str, value: object,
) -> None:
    container = startup_container
    await _create_event(container)
    path, files = _close_and_files(container)
    _rewrite_owner(path, column, value)
    before, raw = _catalog_dump(path), path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="navigation queue owner 原始记录损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("operation", ["acquire", "require"])
@pytest.mark.parametrize("column,value", [
    ("owner_generation", 1.5),
    ("updated_at", "2026-10-23T00:00:00"),
])
async def test_live_owner_read_rejects_corruption_before_fencing_or_takeover(
    startup_container: AppContainer, operation: str, column: str, value: object,
) -> None:
    container = startup_container
    await _create_event(container)
    catalog = container.session_catalog_service
    store = catalog.path_resolver.catalog_store
    with store.read_transaction() as connection:
        original = connection.execute("SELECT owner_id, owner_generation FROM navigation_queue_owners").fetchone()
    _rewrite_owner(store.database_path, column, value)
    # 在同一个外层事务捕获异常，证明拒绝发生在修改前，而不只依赖异常回滚。
    with store.write_transaction() as connection:
        before = "\n".join(connection.iterdump())
        with pytest.raises((TypeError, ValueError), match="navigation queue owner"):
            if operation == "acquire":
                NavigationMutationQueueStore.acquire_owner(
                    connection, workspace_id=catalog.workspace_id, owner_id="new-worker",
                    now=utc_now_text(),
                )
            else:
                NavigationMutationQueueStore.require_owner_generation(
                    connection, workspace_id=catalog.workspace_id, owner_id=original["owner_id"],
                    generation=original["owner_generation"],
                )
        assert "\n".join(connection.iterdump()) == before


async def test_healthy_owner_clock_rollback_preserves_generation_until_real_takeover(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    await _create_event(container)
    path, files = _close_and_files(container)
    _rewrite_owner(path, "updated_at", "2025-01-01T00:00:00+08:00")
    before = _catalog_dump(path)
    with closing(sqlite3.connect(path)) as connection:
        old_id, old_generation = connection.execute(
            "SELECT owner_id, owner_generation FROM navigation_queue_owners",
        ).fetchone()
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    catalog = restarted.session_catalog_service
    await catalog.operations.start()
    try:
        store = catalog.path_resolver.catalog_store
        with store.read_transaction() as connection:
            current = connection.execute("SELECT owner_id, owner_generation FROM navigation_queue_owners").fetchone()
            assert current["owner_id"] != old_id
            assert current["owner_generation"] == old_generation + 1
            with pytest.raises(NavigationQueueOwnerError, match="fencing token 已失效"):
                NavigationMutationQueueStore.require_owner_generation(
                    connection, workspace_id=catalog.workspace_id, owner_id=old_id, generation=old_generation,
                )
    finally:
        await catalog.operations.stop()
    _assert_body_unchanged(container, files)
