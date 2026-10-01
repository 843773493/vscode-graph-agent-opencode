"""gate 内幂等重建 SQLite 目标树并全量对账。"""

from __future__ import annotations

import sqlite3

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_lifecycle_gate import NavigationTopologyGate

from ._contracts import SessionCatalogMigrationError, _MigrationContext


class SessionCatalogMigratorCatalogMixin:
    """gate 内幂等重建 SQLite 目标树并全量对账。"""

    async def _rebuild_catalog_in_gate(self, context: _MigrationContext) -> None:
        """gate 内幂等重建 SQLite 目标树并全量对账(复用切片1 语义)。"""
        gate = NavigationTopologyGate(self._sessions_root)
        async with gate.exclusive():
            try:
                store = SessionCatalogStore(
                    self._database_path, self._sessions_root
                )
            except (
                RuntimeError,
                sqlite3.Error,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                raise self._fail(
                    "sqlite-重建", f"session catalog store 构造失败: {error}"
                ) from error
            try:
                for item in context.frozen:
                    self._create_or_verify_node(store, item)
                self._reconcile(store, context.frozen)
                store.verify_workspace_consistency()
            except SessionCatalogMigrationError:
                raise
            except (
                RuntimeError,
                KeyError,
                sqlite3.Error,
                TypeError,
                ValueError,
                OSError,
            ) as error:
                raise self._fail(
                    "sqlite-重建",
                    "SQLite 目标树重建/对账失败(库保持当前状态,"
                    f"可从 journal 恢复): {error}",
                ) from error
            finally:
                store.close()
