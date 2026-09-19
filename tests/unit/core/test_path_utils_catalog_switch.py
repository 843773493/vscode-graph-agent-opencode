"""path_utils 会话目录 resolver 切换开关测试（8.2-切片3b-1 + R19 默认切换）。

R19 起语义反转：**默认（未设置/任意非 legacy 值）→ 新 catalog resolver**；
仅显式 legacy 值（``"0"``/``"legacy"``）→ 旧 JSON index resolver（迁移期
显式 opt-in）。覆盖：默认值族 → 新 resolver；显式 legacy 值 → 旧 resolver
（每个值独立 sessions_root 构造，防 lru_cache 掩蔽开关读取）；SQLite 存在
→ 新 resolver；旧 index 无 SQLite → RuntimeError（含 SessionCatalogMigrator
迁移提示）；全新 → 空 catalog 初始化；lru_cache 复用与模式固定；
workspace_id 接线（含 resolver 实际绑定值）；便捷函数经工厂。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.path_utils import (
    get_session_file,
    get_session_path,
    get_session_path_resolver,
    initialize_directories,
)
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_paths import SessionPathResolver

SWITCH_ENV = "BOXTEAM_SESSION_CATALOG_RESOLVER"


def make_standard_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """构造标准布局工作区，返回 (workspace_root, sessions_root)。"""
    workspace_root = tmp_path / "workspace"
    sessions_root = workspace_root / ".boxteam" / "sessions"
    return workspace_root, sessions_root


def seed_legacy_index(sessions_root: Path) -> Path:
    """写入旧形态权威索引（含一条合法 v3 记录）。"""
    index_path = sessions_root.parent / "navigation" / "session-catalog-index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "revision": 1,
                "nodes": [
                    {
                        "node_id": "ses_legacy000000000000000000000000",
                        "kind": "session",
                        "name": "旧索引会话",
                        "parent_node_id": None,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return index_path


def seed_corrupt_catalog(sessions_root: Path) -> Path:
    """写入 user_version 非法的 SQLite catalog（fail-closed 探测对象）。"""
    database_path = (
        sessions_root.parent / "navigation" / "session-catalog.sqlite"
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA user_version = 99")
    finally:
        connection.close()
    return database_path


# ----------------------------------------------------------------------
# legacy 显式 opt-in（R19 起默认已是 catalog）
# ----------------------------------------------------------------------


class TestLegacyOptIn:
    def test_env_unset_returns_catalog_resolver(self, tmp_path, monkeypatch):
        """R19 默认切换：环境变量未设置 → 新 catalog resolver（默认权威）。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)

    def test_legacy_values_return_legacy_resolver(self, tmp_path, monkeypatch):
        """仅显式 legacy 值（"0"/"legacy"）返回旧 resolver：锁定 opt-in 闭集。

        每个值都在独立 sessions_root 上构造：lru_cache 以根目录为键，若
        多个值共用同一根，首值构造的实例会被后续值直接命中缓存、绕过开
        关读取（R16 审查 M1 实证：同根循环使放宽变异逃逸全部用例）。
        独立根保证每个值真实触发一次开关判定。
        """
        for index, value in enumerate(("0", "legacy")):
            monkeypatch.setenv(SWITCH_ENV, value)
            # 独立根目录构造，杜绝 lru_cache 跨值复用掩蔽开关读取。
            _, sessions_root = make_standard_workspace(
                tmp_path / f"legacy-{index}"
            )

            resolver = get_session_path_resolver(sessions_root)

            assert isinstance(resolver, SessionPathResolver), (
                f"开关值 {value!r} 是显式 legacy opt-in，应返回旧 resolver"
            )
            assert not isinstance(resolver, SessionCatalogPathResolver)

    def test_non_legacy_values_default_to_catalog_resolver(
        self, tmp_path, monkeypatch
    ):
        """非 legacy 值（含历史 "1" 与任意其它值）一律默认新 catalog resolver。

        未设置（None）也在此锁定：默认权威是 catalog，任何非 legacy 值
        都不回退旧 resolver。独立根目录构造防 lru_cache 掩蔽。
        """
        for index, value in enumerate((None, "1", "", "true", "yes", "on", "0 ")):
            if value is None:
                monkeypatch.delenv(SWITCH_ENV, raising=False)
            else:
                monkeypatch.setenv(SWITCH_ENV, value)
            _, sessions_root = make_standard_workspace(
                tmp_path / f"catalog-{index}"
            )

            resolver = get_session_path_resolver(sessions_root)

            assert isinstance(resolver, SessionCatalogPathResolver), (
                f"开关值 {value!r} 非 legacy opt-in，应默认返回新 catalog resolver"
            )

    def test_legacy_mode_still_serves_legacy_index(self, tmp_path, monkeypatch):
        """显式 legacy 模式下旧 index 仍可读（迁移期行为验证能力保留）。"""
        monkeypatch.setenv(SWITCH_ENV, "0")
        _, sessions_root = make_standard_workspace(tmp_path)
        seed_legacy_index(sessions_root)

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionPathResolver)
        assert resolver.initialize() is None


# ----------------------------------------------------------------------
# 默认（catalog）模式：探测与构造
# ----------------------------------------------------------------------


class TestCatalogDefault:
    def test_existing_sqlite_returns_catalog_resolver(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)
        database_path = (
            sessions_root.parent / "navigation" / "session-catalog.sqlite"
        )
        # 预置已初始化的 catalog（模拟迁移已完成的工作区）。
        from app.core.session_catalog_store import SessionCatalogStore

        store = SessionCatalogStore(database_path, sessions_root)
        store.close()

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)
        # 兼容属性：index_path 指向 SQLite catalog 数据库文件。
        assert resolver.index_path == database_path.resolve()

    def test_sqlite_wins_over_stale_legacy_index(self, tmp_path, monkeypatch):
        """迁移终态允许两者并存：SQLite 存在即以新 resolver 为准。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)
        database_path = (
            sessions_root.parent / "navigation" / "session-catalog.sqlite"
        )
        from app.core.session_catalog_store import SessionCatalogStore

        store = SessionCatalogStore(database_path, sessions_root)
        store.close()
        seed_legacy_index(sessions_root)

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)

    def test_legacy_index_without_sqlite_raises_migration_hint(
        self, tmp_path, monkeypatch
    ):
        """默认模式遇到「旧 index 在场而 SQLite 未建」fail closed（提示迁移）。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)
        legacy_index = seed_legacy_index(sessions_root)

        with pytest.raises(RuntimeError, match="SessionCatalogMigrator"):
            get_session_path_resolver(sessions_root)
        # 旧索引保持原样（不双读、不静默迁移、不删除）。
        assert legacy_index.is_file()

    def test_fresh_workspace_initializes_empty_catalog(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)
        database_path = (
            sessions_root.parent / "navigation" / "session-catalog.sqlite"
        )
        assert database_path.is_file()
        # 空 catalog 初始化：无节点且一致性校验通过。
        assert resolver.list_nodes() == []
        resolver.initialize()

    def test_corrupt_catalog_user_version_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)
        seed_corrupt_catalog(sessions_root)

        with pytest.raises(RuntimeError, match="schema 版本未知"):
            get_session_path_resolver(sessions_root)

    def test_workspace_identity_is_created_and_bound(self, tmp_path, monkeypatch):
        """workspace_id 取自 .boxteam/workspace-identity.json（与 container 同源）。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        workspace_root, sessions_root = make_standard_workspace(tmp_path)
        from app.core.workspace_identity import load_or_create_workspace_id

        expected_id = load_or_create_workspace_id(workspace_root)

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)
        # resolver 实际绑定该 workspace_id（不只 identity 文件内容一致；
        # R16 审查 M3 实证：仅断言文件内容时，identity 落点变异可逃逸）。
        assert resolver._workspace_id == expected_id
        identity = json.loads(
            (workspace_root / ".boxteam" / "workspace-identity.json").read_text(
                encoding="utf-8"
            )
        )
        assert identity["workspace_id"] == expected_id

    def test_convenience_functions_route_through_factory(self, tmp_path, monkeypatch):
        """get_session_path 等便捷函数经工厂使用新 resolver 解析。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        workspace_root, _ = make_standard_workspace(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
        initialize_directories()

        resolver = get_session_path_resolver()
        # R17：allocate 的 session_id 需满足 canonical 形态（占位 ID 亦然，
        # 非 canonical 会被 honor 校验直接拒绝）。
        session_dir = resolver.allocate_session_dir(
            session_id="ses_7c9c9b4ad4c54c0eb9dcd4dabb96e67d",
            title="便捷函数会话",
        )
        marker = json.loads(
            (session_dir / ".boxteam-session-allocating.json").read_text(
                encoding="utf-8"
            )
        )
        session_id = str(marker["session_id"])
        now = "2026-01-01T00:00:00+00:00"
        # workspace_id 取自工厂链路的 identity 文件（与 resolver 同源）。
        from app.core.workspace_identity import load_or_create_workspace_id

        workspace_id = load_or_create_workspace_id(workspace_root)
        # resolver 实际绑定该 workspace_id（R16 审查 N1 补强）。
        assert resolver._workspace_id == workspace_id
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                    "created_at": now,
                    "updated_at": now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)

        assert get_session_path(session_id) == session_dir
        assert get_session_file(session_id) == session_dir / "session.json"

    def test_non_standard_sessions_root_uses_diverted_navigation(
        self, tmp_path, monkeypatch
    ):
        """非标准根目录（测试/嵌入式约定）沿用旧 resolver 的索引目录规则。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        sessions_root = tmp_path / "custom-sessions"

        resolver = get_session_path_resolver(sessions_root)

        assert isinstance(resolver, SessionCatalogPathResolver)
        database_path = (
            tmp_path
            / ".custom-sessions-session-navigation"
            / "session-catalog.sqlite"
        )
        assert database_path.is_file()
        assert resolver.list_nodes() == []


# ----------------------------------------------------------------------
# lru_cache 语义
# ----------------------------------------------------------------------


class TestResolverCache:
    def test_same_root_reuses_cached_instance(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)

        first = get_session_path_resolver(sessions_root)
        second = get_session_path_resolver(sessions_root)

        assert first is second

    def test_mode_fixed_after_first_construction(self, tmp_path, monkeypatch):
        """legacy opt-in 读取在进程内首次构造后固定：后改环境变量不影响既有根。"""
        monkeypatch.delenv(SWITCH_ENV, raising=False)
        _, sessions_root = make_standard_workspace(tmp_path)

        catalog = get_session_path_resolver(sessions_root)
        monkeypatch.setenv(SWITCH_ENV, "0")
        again = get_session_path_resolver(sessions_root)

        assert again is catalog
        assert isinstance(again, SessionCatalogPathResolver)
