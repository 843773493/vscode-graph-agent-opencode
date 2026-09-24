"""8.1-D：pinned ForkRetentionClaim 经 fork metadata owner 的生命周期测试。

覆盖：capture 前建立 preparing claim、target 提交后激活同一 claim、target
删除时按 target 释放；source 已 deleting 时 claim 准入零副作用失败；claim
存在时整树删除在 catalog deleting 提交前返回 blocker。真实 SQLite 只落
``tmp_path``，不触碰真实工作区。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.session_catalog_store import (
    SessionCatalogStore,
    SourceRetentionOperationPendingError,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

SOURCE_SESSION_ID = "ses_2f9d1e4c7a8b4f2d9c3e5a7b1d4f6081"
TARGET_SESSION_ID = "ses_5b8e2f1a6c3d4e7f9a0b2c4d6e8f1357"
CLAIM_ID = "forkclaim0001"


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    """隔离 sessions 根，含 source 与 target 两个 catalog session 节点。"""
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SOURCE_SESSION_ID, title="pin 源会话")
    seed_catalog_session_bundle(
        root, TARGET_SESSION_ID, title="pin 目标会话", parent_node_id=SOURCE_SESSION_ID
    )
    return root


@pytest.fixture
def storage(sessions_root: Path, monkeypatch: pytest.MonkeyPatch) -> RolloutStorage:
    monkeypatch.setenv("WORKSPACE_ROOT", str(sessions_root.parent.parent))
    return RolloutStorage(sessions_root)


def _catalog(sessions_root: Path) -> SessionCatalogStore:
    return SessionCatalogStore(
        sessions_root.parent / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )


def test_begin_pinned_claim_creates_preparing_on_catalog(
    storage: RolloutStorage, sessions_root: Path
) -> None:
    claim = storage.begin_pinned_fork_retention_claim(
        claim_id=CLAIM_ID,
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        source_lifecycle_generation=1,
    )
    assert claim is not None
    assert claim.state == "preparing"
    assert claim.source_session_id == SOURCE_SESSION_ID
    # durable：独立打开 catalog 可见。
    catalog = _catalog(sessions_root)
    try:
        assert catalog.get_fork_retention_claim(CLAIM_ID).state == "preparing"
    finally:
        catalog.close()


def test_activate_then_release_claim_by_target(
    storage: RolloutStorage, sessions_root: Path
) -> None:
    storage.begin_pinned_fork_retention_claim(
        claim_id=CLAIM_ID,
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        source_lifecycle_generation=1,
    )
    activated = storage.activate_pinned_fork_retention_claim(
        CLAIM_ID, expected_generation=1
    )
    assert activated is not None and activated.state == "active"
    catalog = _catalog(sessions_root)
    try:
        # target 删除 → 释放精确 claim。
        storage.release_fork_retentions(TARGET_SESSION_ID)
        assert catalog.get_fork_retention_claim(CLAIM_ID).state == "released"
        assert catalog.list_pinned_claims_for_source(SOURCE_SESSION_ID) == []
    finally:
        catalog.close()


def test_claim_rejected_when_source_deleting(
    storage: RolloutStorage, sessions_root: Path
) -> None:
    catalog = _catalog(sessions_root)
    try:
        catalog.set_node_state(SOURCE_SESSION_ID, "deleting")
    finally:
        catalog.close()
    with pytest.raises(RuntimeError, match="零副作用失败"):
        storage.begin_pinned_fork_retention_claim(
            claim_id=CLAIM_ID,
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            source_lifecycle_generation=1,
        )
    catalog = _catalog(sessions_root)
    try:
        assert catalog.list_pinned_claims_for_source(SOURCE_SESSION_ID) == []
    finally:
        catalog.close()


def test_claim_blocks_subtree_delete_before_commit(
    storage: RolloutStorage, sessions_root: Path
) -> None:
    storage.begin_pinned_fork_retention_claim(
        claim_id=CLAIM_ID,
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        source_lifecycle_generation=1,
    )
    catalog = _catalog(sessions_root)
    try:
        workspace_id = catalog.get_node(SOURCE_SESSION_ID).workspace_id
        catalog.create_or_get_subtree_delete_record(
            idempotency_key="delete-op",
            workspace_id=workspace_id,
            root_node_id=SOURCE_SESSION_ID,
        )
        with pytest.raises(SourceRetentionOperationPendingError):
            catalog.mark_subtree_deleting("delete-op")
        # 整树保持 active。
        assert catalog.get_node(SOURCE_SESSION_ID).state == "active"
        assert catalog.get_node(TARGET_SESSION_ID).state == "active"
    finally:
        catalog.close()


def test_source_generation_read_fails_closed_without_control_db(
    tmp_path: Path,
) -> None:
    """无 control 库时 claim 准入 fail closed，绝不用虚假 generation。"""
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SOURCE_SESSION_ID, title="无 control")
    storage = RolloutStorage(root)
    # 手工删除 control 库模拟缺失。
    from app.core.path_utils import get_session_path_resolver

    session_dir = get_session_path_resolver(root).resolve_session_node(
        SOURCE_SESSION_ID
    )
    (session_dir / "session-control.sqlite").unlink()
    with pytest.raises(RuntimeError, match="session-control.sqlite"):
        storage._source_lifecycle_generation(SOURCE_SESSION_ID)
