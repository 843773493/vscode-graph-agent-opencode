"""retention claim 的局部存储合同；完整 pinned fork 另由真实执行恢复测试验收。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_catalog_startup_workspace_owner import (
    _create_owner_record,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


@pytest.mark.parametrize("state,column,value", [
    ("preparing", "fork_retention_claim_id", ""),
    ("released", "fork_retention_claim_id", None),
    ("active", "source_session_id", "ses-invalid-source"),
    ("released", "target_session_id", "ses-invalid-target"),
    ("preparing", "source_lifecycle_generation", 1.5),
    ("released", "source_lifecycle_generation", "invalid-generation"),
    ("preparing", "record_created_at", "invalid-time"),
    ("active", "record_updated_at", "2026-10-23T00:00:00"),
    ("released", "record_created_at", "2026-10-23T00:00:00"),
    ("released", "record_updated_at", "invalid-time"),
    ("released", "release_reason", None),
    ("released", "release_reason", ""),
    ("preparing", "release_reason", "尚未释放"),
    ("active", "release_reason", "尚未释放"),
])
async def test_container_rejects_unrecoverable_fork_retention_claim(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str, column: str, value: object,
) -> None:
    container = startup_container
    _, _, key = await _create_owner_record(container, monkeypatch, "claim", state)
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE fork_retention_claims SET {column}=? WHERE fork_retention_claim_id=?", (value, key))
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="fork retention claim 损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("state", ["preparing", "active", "released"])
async def test_container_preserves_frozen_retention_claim_after_navigation_changes(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str,
) -> None:
    container = startup_container
    _, _, key = await _create_owner_record(container, monkeypatch, "claim", state)
    resolver = container.session_service.path_resolver
    store = resolver.catalog_store
    claim = store.get_fork_retention_claim(key)
    # 冻结 generation 只要求 writer 的整数合同；不推断它等于当前 fence。
    second = store.create_or_get_fork_retention_claim(
        claim_id="claim/with-slash", workspace_id=container.workspace_id,
        source_session_id=claim.source_session_id, target_session_id=claim.target_session_id,
        source_lifecycle_generation=0,
    )
    store.release_fork_retention_claim(second.fork_retention_claim_id, "局部合同验证完成")
    store.rename_node(claim.source_session_id, "来源合法改名")
    store.rename_node(claim.target_session_id, "目标合法改名")
    if state == "released":
        await resolver.delete_subtree(idempotency_key="delete-released-claim-source", root_node_id=claim.source_session_id)
    path, files = _close_and_files(container)
    # 仅改变合法记账文本，不能因时区或墙钟回拨而重写历史。
    changed_at = "2020-01-01T03:00:00+03:00"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE fork_retention_claims SET record_updated_at=? WHERE fork_retention_claim_id=?", (changed_at, key))
        connection.commit()
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    cold = restarted.session_service.path_resolver.catalog_store
    restored = cold.get_fork_retention_claim(key)
    assert (restored.fork_retention_claim_id, restored.source_session_id, restored.target_session_id,
            restored.source_lifecycle_generation, restored.state, restored.release_reason) == (
        claim.fork_retention_claim_id, claim.source_session_id, claim.target_session_id,
        claim.source_lifecycle_generation, claim.state, claim.release_reason,
    )
    assert restored.record_created_at == claim.record_created_at and restored.record_updated_at == changed_at
    assert cold.get_fork_retention_claim(second.fork_retention_claim_id).source_lifecycle_generation == 0
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)
