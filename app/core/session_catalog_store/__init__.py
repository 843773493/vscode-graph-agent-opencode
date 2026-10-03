"""session-catalog.sqlite 基础设施 facade（单点实现，按垂直链路拆包）。

原先的单文件 ``session_catalog_store.py`` 已按垂直链路拆入本包：形态验证器
与生命周期栅栏（``validators.py``）、DTO/错误/记录解析 helper
（``contracts.py``）、nodes 表读写（``nodes.py``）、fork retention claim
（``fork_retention.py``）、creation record journal（``creation_journal.py``）、
subtree delete journal 与空 folder 删除（``subtree_delete.py``）、只读查询
（``queries.py``）、备份与目录一致性（``backup.py``），共享 DDL 与列清单收敛在
``_schema.py``。

本 facade 只保留 ``SessionCatalogStore`` 的类声明（六个既有 mixin）与连接
生命周期方法（``__init__``/``connection``/``close``/``_ensure_open``/
``_connect``/``_initialize``/``_verify_quick_integrity``/
``_require_tables_present``/``write_transaction``/``_bump_generation``/
``current_generation``/``read_transaction``），并按原名再导出模块级公开符号，
对外契约与导入路径保持不变：``app.core.session_catalog_store.SessionCatalogStore``。

错误分类约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态非法、
``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.session_catalog_store._schema import (
    _CATALOG_METADATA_SEED,
    _CATALOG_METADATA_TABLE_DDL,
    _CREATION_RECORDS_TABLE_DDL,
    _FORK_RETENTION_CLAIMS_TABLE_DDL,
    _IDX_CREATION_RECORDS_STATE_DDL,
    _IDX_FORK_RETENTION_SOURCE_DDL,
    _IDX_NODES_PARENT_DDL,
    _IDX_NODES_WORKSPACE_DDL,
    _IDX_SUBTREE_DELETE_STATE_DDL,
    _NODES_TABLE_DDL,
    _REQUIRED_TABLES_BY_VERSION,
    _SUBTREE_DELETE_RECORDS_TABLE_DDL,
)
from app.core.session_catalog_store.backup import CatalogBackupMixin
from app.core.session_catalog_store.contracts import (
    CatalogBackupManifest,
    CatalogIntegrityReport,
    CatalogMaintenanceRequiredError,
    ForkRetentionClaim,
    SessionCatalogNode,
    SessionCreationRecord,
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
    SubtreeDeleteRecord,
    SubtreeFrozenNode,
)
from app.core.session_catalog_store.creation_journal import CreationJournalMixin
from app.core.session_catalog_store.fork_retention import ForkRetentionMixin
from app.core.session_catalog_store.nodes import CatalogNodesMixin
from app.core.session_catalog_store.queries import CatalogQueriesMixin
from app.core.session_catalog_store.subtree_delete import SubtreeDeleteMixin
from app.core.session_catalog_store.validators import (
    SessionLifecycleFence,
    uuid7_embedded_utc_date,
    validate_path_budget,
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)
from app.core.sqlite_state import (
    SQLITE_BUSY_TIMEOUT_MS,
    validate_sqlite_path_budget,
)

__all__ = [
    "CatalogBackupManifest",
    "CatalogIntegrityReport",
    "CatalogMaintenanceRequiredError",
    "ForkRetentionClaim",
    "SessionCatalogNode",
    "SessionCatalogStore",
    "SessionCreationRecord",
    "SessionLifecycleFence",
    "SourceRetainedByForkError",
    "SourceRetentionOperationPendingError",
    "SubtreeDeleteRecord",
    "SubtreeFrozenNode",
    "uuid7_embedded_utc_date",
    "validate_path_budget",
    "validate_session_id",
    "validate_storage_relative_locator",
    "validate_thread_id",
]


class SessionCatalogStore(
    CatalogNodesMixin,
    ForkRetentionMixin,
    CreationJournalMixin,
    SubtreeDeleteMixin,
    CatalogQueriesMixin,
    CatalogBackupMixin,
):
    """session-catalog.sqlite 的 nodes 表与 creation record journal 基础设施。

    SQLite ``nodes`` 表是唯一目录权威。构造时创建 database_path 的父目录，
    但**不创建** sessions_root 目录（store 只管 catalog，不管物理树）。
    创建和删除 journal 与 nodes 同库，由事务保证目录可见性和恢复进度的一致性。
    """

    SCHEMA_VERSION = 3

    def __init__(self, database_path: Path, sessions_root: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.sessions_root = sessions_root.expanduser().resolve()
        validate_sqlite_path_budget(self.database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        # R18-3：单连接跨线程串行锁（可重入）。本 store 只持有一条共享
        # sqlite3 连接（check_same_thread=False），历史实现无任何锁——跨
        # 线程并发进入 read_transaction/write_transaction 会在同一连接
        # 上交叠出 sqlite3.OperationalError（读侧 BEGIN：
        # cannot start a transaction within a transaction；交叠窗口内的
        # COMMIT 侧变体：cannot commit - no transaction is active，见
        # R17 审查 §E6 与实测复现）。所有连接访问统一在锁内串行；RLock
        # 可重入（同线程事务体内再走本类方法/锁内取 connection 属性），
        # 单线程行为与此前逐行等价。SQLite 操作短，串行化代价可忽略。
        self._connection_lock = threading.RLock()
        self._connection = self._connect()
        try:
            with self._connection_lock:
                self._initialize()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接，仅供诊断与测试直接注入使用；生产写入必须走本类方法。

        R18-3：属性读取（含关闭状态校验）在连接串行锁内完成（锁内暴露）。
        调用方取得连接后的直接 SQL 使用须保持单线程或自行外部同步——绕过
        本类方法的跨线程裸用不在串行化保护范围内（与既有使用约定一致）。
        """
        with self._connection_lock:
            self._ensure_open()
            return self._connection

    def close(self) -> None:
        """关闭底层连接；重复 close 是幂等 no-op。

        R18-3：close 与进行中的事务在锁上串行——他线程事务未结束时
        close 阻塞至其提交/回滚后再关闭（同线程行为不变）。
        """
        with self._connection_lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"session catalog store 已关闭: {self.database_path}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            # 已存在的库若页结构损坏（被截断/覆写），``PRAGMA`` 本身即抛
            # ``sqlite3.DatabaseError``；统一转成维护模式错误，绝不允许以
            # 半损坏的权威表继续读写（8.1-F「绝不默默失败」）。
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            )
        except sqlite3.DatabaseError as error:
            connection.close()
            raise CatalogMaintenanceRequiredError(
                "session catalog 无法打开（文件损坏或被外部改写，须从备份恢复"
                f"或进入维护模式核对）: path={self.database_path}: {error}"
            ) from error
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                "session catalog 无法启用 WAL: "
                f"path={self.database_path}, journal_mode={journal_mode}"
            )
        return connection

    def _initialize(self) -> None:
        """幂等建表并设置 user_version；未知版本 fail-closed 拒绝打开。

        支持的 current：0（全新库，建全部表并置 v3）、1（R10 v1 库）、
        2（R14 v2 库）、3（当前版本）。1/2→3 是**显式一次性 schema
        迁移**：同一 ``BEGIN IMMEDIATE`` 事务内加法建
        ``fork_retention_claims``/``catalog_metadata`` 两张表并升 v3，不动
        既有表 DDL 与数据、无动态分支。``subtree_delete_records`` 是 R14
        加法补表（不改 user_version），每次打开幂等补建。
        """
        current = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current > self.SCHEMA_VERSION:
            raise RuntimeError(
                "session catalog schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        if current:
            self._verify_quick_integrity()
            self._require_tables_present(current)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(_NODES_TABLE_DDL)
            self._connection.execute(_IDX_NODES_PARENT_DDL)
            self._connection.execute(_IDX_NODES_WORKSPACE_DDL)
            self._connection.execute(_CREATION_RECORDS_TABLE_DDL)
            self._connection.execute(_IDX_CREATION_RECORDS_STATE_DDL)
            self._connection.execute(_SUBTREE_DELETE_RECORDS_TABLE_DDL)
            self._connection.execute(_IDX_SUBTREE_DELETE_STATE_DDL)
            self._connection.execute(_FORK_RETENTION_CLAIMS_TABLE_DDL)
            self._connection.execute(_IDX_FORK_RETENTION_SOURCE_DDL)
            self._connection.execute(_CATALOG_METADATA_TABLE_DDL)
            self._connection.execute(_CATALOG_METADATA_SEED)
            if current != self.SCHEMA_VERSION:
                self._connection.execute(
                    f"PRAGMA user_version = {self.SCHEMA_VERSION}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _verify_quick_integrity(self) -> None:
        """启动期 ``PRAGMA quick_check``：catalog 损坏时 fail closed（8.1-F）。

        只对已存在的库执行。返回非 ``ok`` 说明页级损坏，绝不允许继续以
        半损坏的权威表读写——立刻抛错，由 operator 从备份恢复或进入维护
        模式核对，不得扫盘重建。
        """
        result = self._connection.execute("PRAGMA quick_check").fetchone()
        status = str(result[0]) if result is not None else "<empty>"
        if status.lower() != "ok":
            raise CatalogMaintenanceRequiredError(
                "session catalog 完整性校验失败，拒绝打开（须从备份恢复或进入"
                "维护模式核对，不得扫盘重建）: "
                f"path={self.database_path}, quick_check={status!r}"
            )

    def _require_tables_present(self, current: int) -> None:
        """校验已登记的版本对应表确实存在；缺表说明库被外部改写。

        绝不允许用 ``CREATE TABLE IF NOT EXISTS`` 把权威表当空表重建：
        ``nodes`` 一空，所有会话位置与父子关系即静默丢失。缺表一律响亮
        失败并列出缺失表名，交由用户从备份恢复或执行维护迁移。
        """
        present = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = [
            table
            for table in _REQUIRED_TABLES_BY_VERSION.get(current, ())
            if table not in present
        ]
        if missing:
            raise RuntimeError(
                "session catalog 缺表，拒绝以空表重建（库被外部改写）: "
                f"path={self.database_path}, user_version={current}, "
                f"missing={missing}"
            )

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE 内先验证后写入，异常回滚。

        R18-3：事务全程（BEGIN → yield 出的语句执行窗口 → COMMIT/
        ROLLBACK）持有连接串行锁——跨线程的后到事务阻塞至先到事务结束，
        共享连接上不再出现事务交叠（OperationalError 两种变体的根源）。
        SQL 语义与 BEGIN 模式不变；RLock 可重入，同线程嵌套安全。

        提交前自增 ``catalog_metadata.generation``（8.1-F）：每个提交的写
        事务恰好推进一次 generation，作为备份/一致性快照的版本锚点。

        公开入口：调用方可在自己持有的连接上执行 node 写方法与同库旁挂
        journal/事件写入，由本事务一次性提交或回滚。
        """
        with self._connection_lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._bump_generation(self._connection)
            self._connection.execute("COMMIT")

    @staticmethod
    def _bump_generation(connection: sqlite3.Connection) -> None:
        """写事务提交前自增单调 generation（8.1-F）。"""
        cursor = connection.execute(
            "UPDATE catalog_metadata SET generation = generation + 1 "
            "WHERE singleton_id = 1"
        )
        if cursor.rowcount != 1:
            raise CatalogMaintenanceRequiredError(
                "session catalog 缺少 catalog_metadata 单例行，拒绝提交写事务"
                "（库被外部改动，须进入维护模式核对）"
            )

    def current_generation(self) -> int:
        """返回 catalog 当前单调 generation（备份/一致性快照的版本锚点）。"""
        with self.read_transaction() as connection:
            row = connection.execute(
                "SELECT generation FROM catalog_metadata WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise CatalogMaintenanceRequiredError(
                    "session catalog 缺少 catalog_metadata 单例行（库被外部改动，"
                    f"须进入维护模式核对）: path={self.database_path}"
                )
            return int(row[0])

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个读事务：多语句读共享同一快照。

        R18-3：事务全程持有连接串行锁（语义同 ``write_transaction``），
        跨线程读/写/读在共享连接上完全串行，不再交叠。

        公开入口：只读单事务快照，不推进 generation。
        """
        with self._connection_lock:
            self._ensure_open()
            self._connection.execute("BEGIN DEFERRED")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
