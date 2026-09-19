"""OwnerThreadCreationFactory 的 resolver 模式边界测试。

legacy opt-in（`BOXTEAM_SESSION_CATALOG_RESOLVER=0|legacy`）时工厂构造
必须成功（否则整个后端无法启动），child thread 唯一入口
（for_owner_session / owner_main_thread_id）显式 fail closed；catalog 模式
下入口正常工作。每个用例使用独立 sessions_root，防 lru_cache 掩蔽开关
读取（R16 审查 M1 教训）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_paths import SessionPathResolver
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)

SWITCH_ENV = "BOXTEAM_SESSION_CATALOG_RESOLVER"
SESSION_ID = "ses_0123456789abcdef0123456789abcdef"


def _sessions_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path / label / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    return root


def test_legacy_mode_factory_constructs_and_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = _sessions_root(tmp_path, "legacy-entry")
    monkeypatch.setenv(SWITCH_ENV, "0")
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id="ws_legacy",
    )
    assert isinstance(get_session_path_resolver(sessions_root), SessionPathResolver)
    with pytest.raises(RuntimeError, match="child thread 创建要求新 catalog resolver"):
        factory.for_owner_session(SESSION_ID)
    with pytest.raises(RuntimeError, match="child thread 创建要求新 catalog resolver"):
        factory.owner_main_thread_id(SESSION_ID)


def test_default_mode_factory_uses_catalog_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = _sessions_root(tmp_path, "catalog-default")
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id="ws_catalog",
    )
    assert isinstance(factory._resolver, SessionCatalogPathResolver)
    # catalog 权威下入口不因 resolver 模式拒绝，按 catalog 内容正常工作
    #（空 catalog 查未知节点 → 显式 KeyError）。
    with pytest.raises(KeyError, match="会话目录节点不存在"):
        factory.owner_main_thread_id(SESSION_ID)
