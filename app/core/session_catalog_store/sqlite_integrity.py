"""catalog 启动与备份共用的 SQLite 完整性校验。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.session_catalog_store.contracts import CatalogMaintenanceRequiredError


def verify_catalog_sqlite_integrity(
    connection: sqlite3.Connection, *, database_path: Path, database_label: str,
) -> None:
    """调用方持有同一读快照；只核页、索引、约束与外键，不修复数据。"""
    try:
        statuses = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    except sqlite3.DatabaseError as error:
        raise CatalogMaintenanceRequiredError(
            f"{database_label}损坏或结构不完整，完整性校验失败，须进入维护模式核对并保留原数据: "
            f"path={database_path}, sqlite_error={error}"
        ) from error
    if statuses != ["ok"] or foreign_keys:
        raise CatalogMaintenanceRequiredError(
            f"{database_label}损坏或结构不完整，完整性校验失败，须进入维护模式核对并保留原数据: "
            f"path={database_path}, integrity_check={statuses}, foreign_key_check={foreign_keys}"
        )
