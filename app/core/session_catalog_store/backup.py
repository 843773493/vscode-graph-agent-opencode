"""SQLite online backup 与目录一致性快照一条垂直链路（8.1-F）。

承载一致性备份生成（online backup API + generation/checksum 清单）、备份
核对（checksum/generation fail-closed 进维护模式）、日期桶与 catalog locator
一一对应校验，以及备份 checksum 的 ``_sha256_file`` helper。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_catalog_store.contracts import (
    CatalogBackupManifest,
    CatalogIntegrityReport,
    CatalogMaintenanceRequiredError,
)


class CatalogBackupMixin:
    """备份与目录一致性校验方法族（唯一实现点）。"""

    # ------------------------------------------------------------------
    # SQLite online backup / 一致性快照（8.1-F）
    # ------------------------------------------------------------------

    def create_consistent_backup(
        self,
        backup_path: Path,
    ) -> CatalogBackupManifest:
        """用 SQLite online backup API 生成可校验一致性快照并记录 generation/checksum。

        绝不允许直接复制活动 WAL 文件：``sqlite3.Connection.backup`` 在源库
        持读锁期间复制一致页视图，产出与任何已提交 generation 一致的快照。
        目标必须不存在（不覆盖既有备份）。返回清单的 ``generation`` 是备份
        时刻 catalog generation，``checksum`` 是备份文件 sha256。
        """
        if not isinstance(backup_path, Path):
            raise TypeError(f"backup_path 必须是 Path: {backup_path!r}")
        resolved = backup_path.expanduser().resolve()
        if resolved == self.database_path:
            raise ValueError(f"备份目标不能是 catalog 本体: {resolved}")
        if resolved.exists():
            raise RuntimeError(f"备份目标已存在，拒绝覆盖: {resolved}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        generation = self.current_generation()
        with self._connection_lock:
            self._ensure_open()
            destination = sqlite3.connect(resolved)
            try:
                self._connection.backup(destination)
            finally:
                destination.close()
        checksum = _sha256_file(resolved)
        return CatalogBackupManifest(
            generation=generation,
            checksum=checksum,
            database_path=str(resolved),
            created_at=datetime.now(UTC).isoformat(),
        )

    def verify_consistent_backup(
        self,
        backup_path: Path,
        *,
        expected_checksum: str,
        backup_generation: int,
    ) -> CatalogIntegrityReport:
        """核对一致性备份：checksum 不符或 generation 落后即进入维护模式。

        - checksum 与 ``expected_checksum`` 不符 → ``CatalogMaintenanceRequiredError``
          （备份被外部改动或损坏）；
        - ``backup_generation`` 小于当前 generation → 备份落后于已提交操作，
          ``CatalogMaintenanceRequiredError``；
        - 相等 → 返回核对报告（不修改任何数据）。
        """
        if not isinstance(backup_path, Path):
            raise TypeError(f"backup_path 必须是 Path: {backup_path!r}")
        resolved = backup_path.expanduser().resolve()
        if not resolved.is_file():
            raise CatalogMaintenanceRequiredError(
                f"一致性备份缺失，须进入维护模式核对: {resolved}"
            )
        actual_checksum = _sha256_file(resolved)
        if actual_checksum != expected_checksum:
            raise CatalogMaintenanceRequiredError(
                "一致性备份 checksum 不符（被外部改动或损坏，须进入维护模式"
                "核对并保留原数据）: "
                f"path={resolved}, expected={expected_checksum}, "
                f"actual={actual_checksum}"
            )
        current = self.current_generation()
        if backup_generation < current:
            raise CatalogMaintenanceRequiredError(
                "一致性备份落后于已提交操作（须进入维护模式核对，不得扫盘补齐）: "
                f"backup_generation={backup_generation}, "
                f"current_generation={current}, path={resolved}"
            )
        return CatalogIntegrityReport(
            generation=current,
            quick_check="ok",
            backup_generation=backup_generation,
            backup_checksum=actual_checksum,
        )

    def verify_registered_date_directories(self) -> None:
        """校验 ``sessions/YYYY/MM/DD`` 日期桶与 catalog locator 一一对应（8.1-F）。

        存在**未登记**日期目录（磁盘上有而 catalog 无对应 locator）→
        ``CatalogMaintenanceRequiredError``：只保留原数据、进入维护模式核对，
        绝不扫盘补 active node、绝不 GC 未知目录。反向（catalog 有而磁盘缺）
        由 locator 解析层 fail closed，不在本方法重复。
        """
        if not self.sessions_root.is_dir():
            return
        registered: set[str] = set()
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT storage_relative_locator FROM nodes "
                "WHERE kind = 'session' AND storage_relative_locator IS NOT NULL"
            ).fetchall()
        for row in rows:
            locator = str(row[0])
            parts = locator.split("/")
            registered.add("/".join(parts[1:4]))
        unknown: list[str] = []
        for year_dir in sorted(self.sessions_root.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                # 非日期桶目录（如 .staging/.deleting）不属于本校验面。
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir() or not day_dir.name.isdigit():
                        continue
                    bucket = f"{year_dir.name}/{month_dir.name}/{day_dir.name}"
                    if bucket not in registered:
                        unknown.append(bucket)
        if unknown:
            raise CatalogMaintenanceRequiredError(
                "sessions 下存在未登记日期目录（须进入维护模式核对并保留原数据，"
                "不得扫盘补 active node 或交给 GC）: "
                f"sessions_root={self.sessions_root}, unknown={sorted(unknown)}"
            )



def _sha256_file(path: Path) -> str:
    """计算文件 sha256（备份清单 checksum）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
