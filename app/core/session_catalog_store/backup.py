"""SQLite online backup 与目录一致性快照一条垂直链路（8.1-F）。

承载一致性备份生成（online backup API + generation/checksum 清单）、备份
核对（checksum/generation fail-closed 进维护模式）、日期桶与 catalog locator
一一对应校验，以及备份 checksum 的 ``_sha256_file`` helper。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

from app.core.atomic_fs import native_io_path
from app.core.session_catalog_store.contracts import (
    CatalogBackupManifest,
    CatalogIntegrityReport,
    CatalogMaintenanceRequiredError,
    _parse_frozen_session_locators,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.session_catalog_store.schema_validation import require_catalog_schema
from app.core.session_catalog_store.sqlite_integrity import (
    verify_catalog_sqlite_integrity,
)
from app.core.session_catalog_store.validators import validate_storage_relative_locator
from app.core.sqlite_state import sqlite_file_uri, validate_sqlite_path_budget


def _numeric_child_directories(directory: Path) -> list[Path]:
    directories: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise CatalogMaintenanceRequiredError(
                    "Session 日期目录被替换为链接或非目录，须进入维护模式核对并保留原数据"
                )
            directories.append(Path(entry.path))
    return directories


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
        validate_sqlite_path_budget(resolved)
        if native_io_path(resolved).exists():
            raise RuntimeError(f"备份目标已存在，拒绝覆盖: {resolved}")
        native_io_path(resolved.parent).mkdir(parents=True, exist_ok=True)
        with self._connection_lock:
            self._ensure_open()
            destination = sqlite3.connect(sqlite_file_uri(resolved), uri=True)
            try:
                self._connection.backup(destination)
                # 其它 catalog 连接可在复制前或复制后提交；清单只描述已复制的快照。
                generation = int(destination.execute(
                    "SELECT generation FROM catalog_metadata WHERE singleton_id = 1"
                ).fetchone()[0])
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
        - 清单 generation 与备份正文不一致 → 明确拒绝；
        - 备份 generation 小于当前 generation → 备份落后于已提交操作，
          ``CatalogMaintenanceRequiredError``；
        - 相等 → 返回核对报告（不修改任何数据）。
        """
        if not isinstance(backup_path, Path):
            raise TypeError(f"backup_path 必须是 Path: {backup_path!r}")
        resolved = backup_path.expanduser().resolve()
        if not native_io_path(resolved).is_file():
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
        # 备份是独立的单文件快照；只读且不创建 WAL/SHM，不调用会初始化的 catalog 构造器。
        try:
            snapshot = sqlite3.connect(sqlite_file_uri(resolved) + "?mode=ro&immutable=1", uri=True)
            try:
                _register_session_list_functions(snapshot)
                snapshot.execute("BEGIN DEFERRED")
                verify_catalog_sqlite_integrity(
                    snapshot, database_path=resolved, database_label="一致性备份",
                )
                schema = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
                if schema != self.SCHEMA_VERSION:
                    raise CatalogMaintenanceRequiredError(
                        f"一致性备份 schema 不受支持: actual={schema}, required={self.SCHEMA_VERSION}"
                    )
                require_catalog_schema(
                    snapshot, database_path=resolved, current=schema, database_label="一致性备份",
                )
                row = snapshot.execute("SELECT generation FROM catalog_metadata WHERE singleton_id = 1").fetchone()
                if row is None:
                    raise CatalogMaintenanceRequiredError("一致性备份缺少 generation，须进入维护模式核对")
                copied_generation = int(row[0])
            finally:
                snapshot.close()
        except (sqlite3.DatabaseError, RuntimeError) as error:
            raise CatalogMaintenanceRequiredError(
                f"一致性备份损坏或结构不完整，须进入维护模式核对: {error}"
            ) from error
        if backup_generation != copied_generation:
            raise CatalogMaintenanceRequiredError(
                "清单 generation 与备份正文不一致，须进入维护模式核对: "
                f"manifest={backup_generation}, snapshot={copied_generation}"
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
        """核对日期目录与权威记录；生产调用方须持 topology shared gate。

        preparing creation 可以已经 rename；已完成的创建/删除记录可证明空日期桶
        的来源，但不能授权已删除 Session 的旧目录重新出现。只检查归属，不吸收、
        修复或清理磁盘内容；缺失的已登记正文仍由各自 locator/恢复 owner 检查。
        """
        if not self.sessions_root.is_dir():
            return
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT storage_relative_locator, 1 AS allows_directory FROM nodes WHERE kind='session' "
                "UNION ALL SELECT storage_relative_locator, state='preparing' FROM session_creation_records"
            ).fetchall()
            deletions = connection.execute("SELECT frozen_session_locators FROM subtree_delete_records").fetchall()
        known_locators = {str(row[0]) for row in rows}
        allowed_locators = {str(row[0]) for row in rows if row[1]}
        try:
            for deletion in deletions:
                known_locators.update(_parse_frozen_session_locators(str(deletion[0])).values())
            for locator in known_locators:
                validate_storage_relative_locator(locator)
        except (TypeError, ValueError, RuntimeError) as error:
            raise CatalogMaintenanceRequiredError(
                "Session 日期目录归属记录损坏，须进入维护模式核对并保留原数据"
            ) from error
        registered = {"/".join(locator.split("/")[1:4]) for locator in known_locators}
        unknown: list[str] = []
        # 只枚举日期树，不进入 .staging/.deleting 或私有 fork capture。
        for year_dir in _numeric_child_directories(self.sessions_root):
            for month_dir in _numeric_child_directories(year_dir):
                for day_dir in _numeric_child_directories(month_dir):
                    bucket = f"{year_dir.name}/{month_dir.name}/{day_dir.name}"
                    if bucket not in registered:
                        unknown.append(bucket)
                    with os.scandir(day_dir) as entries:
                        for entry in entries:
                            locator = f"sessions/{bucket}/{entry.name}"
                            if locator not in allowed_locators:
                                unknown.append(locator)
                                continue
                            try:
                                mode = entry.stat(follow_symlinks=False).st_mode
                            except FileNotFoundError:
                                # 删除 drain 在 topology gate 外隔离叶目录；缺失交由 locator owner 核验。
                                continue
                            if not stat.S_ISDIR(mode):
                                unknown.append(locator)
        if unknown:
            raise CatalogMaintenanceRequiredError(
                "sessions 下存在未登记日期目录或 Session 目录（须进入维护模式核对并保留原数据，"
                f"不得扫盘补 active node 或交给 GC）: unknown={sorted(unknown)}"
            )



def _sha256_file(path: Path) -> str:
    """计算文件 sha256（备份清单 checksum）。"""
    digest = hashlib.sha256()
    with native_io_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
