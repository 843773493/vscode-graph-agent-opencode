"""OwnerThreadCreationFactory 的 SQLite catalog authority 契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)

SESSION_ID = "ses_0123456789abcdef0123456789abcdef"


def _sessions_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path / label / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    return root


def test_factory_uses_catalog_resolver(tmp_path: Path) -> None:
    sessions_root = _sessions_root(tmp_path, "catalog-default")
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id="ws_catalog",
    )
    assert isinstance(factory._resolver, SessionCatalogPathResolver)
    # catalog 权威下入口不因 resolver 模式拒绝，按 catalog 内容正常工作
    #（空 catalog 查未知节点 → 显式 KeyError）。
    with pytest.raises(KeyError, match="会话目录节点不存在"):
        factory.owner_main_thread_id(SESSION_ID)
