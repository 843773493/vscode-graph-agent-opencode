"""per-session ``session-control.sqlite`` 的最小基础设施（facade）。

原先的单文件 ``session_control_store.py`` 已按垂直链路拆入本包：thread creation
record（``thread_creation_record.py``）、创建发布与终结（``thread_creation_
publish.py``）、初始 execution intent（``execution_intent.py``）、collaboration
成员账本（``collaboration.py``）、当前 schema 初始化（``_schema.py``），
共用 SQL 常量收敛在 ``sql.py``，GraphBinding 行投影位于 ``contracts.py``。

本 facade 只保留 ``SessionControlStore`` 的类声明（四个既有 mixin + 五个新
mixin）与连接生命周期方法（``__init__``/``connection``/``close``/
``_ensure_open``/``_begin_immediate``/``_write_transaction``/``read_transaction``/``_connect``），
并按原名再导出模块级公开符号，对外契约与导入路径保持不变：
``app.core.session_control_store.SessionControlStore``。

错误分类约定沿用 ``session_catalog_store.py``：``TypeError`` 输入类型错误、
``ValueError`` 输入形态非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义
冲突。连接约定：WAL、``foreign_keys``、``busy_timeout``、``sqlite3.Row``、
未知 ``PRAGMA user_version`` 时 fail-closed 拒绝打开。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.atomic_fs import native_io_path
from app.core.identifier import validate_thread_id as validate_thread_id
from app.core.session_control_communication_ledger.communication_ledger import (
    CommunicationInboxRecord,
    CommunicationLedgerMixin,
    CommunicationOutboxRecord,
    derive_communication_admission_identity,
)
from app.core.session_control_operation_lease.operation_lease import (
    OperationLeaseMixin,
)
from app.core.session_control_primitives import (
    SHA256_HEX_PATTERN as SHA256_HEX_PATTERN,
)
from app.core.session_control_primitives import (
    validate_thread_creation_key as validate_thread_creation_key,
)
from app.core.session_control_store._schema import SchemaInitMixin
from app.core.session_control_store.background_mailbox import BackgroundMailboxMixin
from app.core.session_control_store.background_work import BackgroundWorkMixin
from app.core.session_control_store.collaboration import (
    CollaborationMember as CollaborationMember,
)
from app.core.session_control_store.collaboration import (
    CollaborationMemberInput as CollaborationMemberInput,
)
from app.core.session_control_store.collaboration import (
    CollaborationMixin,
)
from app.core.session_control_store.collaboration_fanout import (
    CollaborationFanoutMixin,
    CollaborationFanoutRecord,
)
from app.core.session_control_store.contracts import (
    ThreadGraphBindingRecord as ThreadGraphBindingRecord,
)
from app.core.session_control_store.execution_intent import (
    ExecutionIntentMixin,
    ThreadExecutionIntent,
    compute_initial_execution_binding_preimage_hash,
    derive_initial_execution_identity,
)
from app.core.session_control_store.thread_creation_publish import (
    ThreadCreationPublishMixin,
)
from app.core.session_control_store.thread_creation_record import (
    _INITIAL_STATE_VALUES as _INITIAL_STATE_VALUES,
)
from app.core.session_control_store.thread_creation_record import (
    ThreadCreationRecord,
    ThreadCreationRecordMixin,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    ThreadCatalogMixin,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    validate_thread_relative_locator as validate_thread_relative_locator,
)
from app.core.session_control_thread_owner_binding.thread_owner_binding import (
    ThreadOwnerBinding,
    ThreadOwnerBindingMixin,
)
from app.core.sqlite_state import (
    SQLITE_BUSY_TIMEOUT_MS,
    sqlite_file_uri,
    validate_sqlite_path_budget,
)

__all__ = [
    "CollaborationMemberInput",
    "CommunicationInboxRecord",
    "CommunicationOutboxRecord",
    "SessionControlStore",
    "ThreadCreationRecord",
    "ThreadExecutionIntent",
    "ThreadGraphBindingRecord",
    "ThreadOwnerBinding",
    "compute_initial_execution_binding_preimage_hash",
    "derive_communication_admission_identity",
    "derive_initial_execution_identity",
]

logger = logging.getLogger(__name__)


def _store_failure(path: object, stage: str, reason: str, error: BaseException | None = None) -> str:
    detail = f"{reason}: artifact=session_control, stage={stage}"
    if error is not None:
        detail += f", error_type={type(error).__name__}"
        for name in ("errno", "winerror", "sqlite_errorcode", "sqlite_errorname"):
            value = getattr(error, name, None)
            if value is not None:
                detail += f", {name}={value}"
    logger.error("%s; path=%s", detail, path, exc_info=error)
    return detail


class SessionControlStore(
    CollaborationFanoutMixin,
    BackgroundWorkMixin,
    BackgroundMailboxMixin,
    CommunicationLedgerMixin,
    OperationLeaseMixin,
    ThreadCatalogMixin,
    ThreadOwnerBindingMixin,
    ThreadCreationRecordMixin,
    ThreadCreationPublishMixin,
    CollaborationMixin,
    SchemaInitMixin,
    ExecutionIntentMixin,
):
    """单 session 控制库：thread catalog（main + child）+ lifecycle fence
    + thread creation record / execution intent journal / 通用 operation
    lease（2.3-E）/ 跨 Session 通信 outbox+inbox ledger（D5）。

    构造时创建 database_path 父目录并幂等建表；``user_version`` 只接受
    全新库0与当前版本。旧生成数据在任何 journal/WAL 设置前显式拒绝，
    不迁移、不回填；当前 GraphBinding 仍由 thread_catalog 唯一持有。
    """

    SCHEMA_VERSION = 18

    def __init__(self, database_path: Path, *, read_only: bool = False) -> None:
        if not isinstance(database_path, Path):
            raise TypeError(_store_failure(database_path, "input", "database_path 必须是 Path"))
        self.database_path = database_path
        self._read_only = read_only
        self._collaboration_notifications: list[int] = []
        self._collaboration_publication: Callable[[CollaborationFanoutRecord], None] | None = None
        self._publication_lock = threading.RLock()
        self._closed = True
        stage = "resolve"
        try:
            self.database_path = database_path.expanduser().resolve()
            stage = "path_budget"
            validate_sqlite_path_budget(self.database_path)
            if not read_only:
                stage = "directory"
                native_io_path(self.database_path.parent).mkdir(parents=True, exist_ok=True)
            stage = "connect"
            self._connect()
            if not read_only:
                stage = "schema_initialize"
                self._initialize()
        except BaseException as error:
            reported = error
            if isinstance(error, (OSError, sqlite3.Error)):
                detail = _store_failure(self.database_path, stage, "session control 原生访问失败", error)
                if isinstance(error, OSError):
                    reported = type(error)(error.errno, detail, None, getattr(error, "winerror", None))
                else:
                    reported = type(error)(detail)
                    for name in ("sqlite_errorcode", "sqlite_errorname"):
                        if hasattr(error, name):
                            setattr(reported, name, getattr(error, name))
            elif stage == "resolve" and isinstance(error, RuntimeError):
                reported = RuntimeError(_store_failure(self.database_path, stage, "session control 路径解析失败", error))
            try:
                self.close()
            except BaseException as close_error:  # noqa: BLE001 - 构造与回收失败均须保留。
                reported.add_note(_store_failure(self.database_path, "close", "session control 构造失败后的连接关闭失败", close_error))
            if reported is error:
                raise
            raise reported from error

    # ------------------------------------------------------------------
    # 连接与 schema
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接，仅供诊断与测试直接注入使用；生产写入走本类方法。"""
        self._ensure_open()
        return self._connection

    def close(self) -> None:
        """关闭底层连接；重复 close 是幂等 no-op。"""
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(_store_failure(self.database_path, "access", "session control store 已关闭"))

    def bind_collaboration_publication(self, callback: Callable[[CollaborationFanoutRecord], None], publication_lock) -> None:
        """writer 与 Registry capture 共用发布锁；通知只镜像已提交 fanout。"""
        if self._connection.in_transaction:
            raise RuntimeError("不能在活动事务中替换 collaboration publication")
        self._collaboration_publication = callback
        self._publication_lock = publication_lock

    def _begin_immediate(self) -> None:
        """开启写事务（BEGIN IMMEDIATE），锁冲突转明确领域错误。

        本库只持一条共享连接，但同一 session 库可能被多个进程同时打开
        （resolver / worker / 删除流各持一份）；跨进程写竞争超过
        ``SQLITE_BUSY_TIMEOUT_MS`` 时 sqlite3 抛裸 ``OperationalError:
        database is locked``。此处公开逻辑位置、原生错误码与等待时长，
        数据库原生路径和异常原件留在诊断日志。

        不变量：不可重入。嵌套调用会在外层事务内再 BEGIN，本方法在
        进入前响亮失败，避免 sqlite3 的 "cannot start a transaction
        within a transaction"（详见 ``_write_transaction``）。
        """
        self._ensure_open()
        if self._read_only:
            raise RuntimeError("只读 operation replay 控制库禁止写事务")
        if self._connection.in_transaction:
            raise RuntimeError(_store_failure(self.database_path, "BEGIN IMMEDIATE",
                "session control 写事务嵌套：_begin_immediate 不可重入"
                "（外层事务未结束，内层 BEGIN IMMEDIATE 会破坏事务边界）",
            ))
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            waited_ms = int(
                self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            reason = "session control 获取写事务失败"
            if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                reason += f"：另一个进程持有写锁，等待 {waited_ms}ms 后仍被占用（fail loud）"
            raise RuntimeError(_store_failure(self.database_path, "BEGIN IMMEDIATE", reason, error)) from error

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE 内先验证后写入。

        正常退出（含事务体内 ``return``）一律 COMMIT，任何异常（包括
        COMMIT 失败）ROLLBACK 后原样抛出——模式对齐
        ``session_catalog_store.write_transaction``，
        吸取 R14 审查 M1 教训：禁止在打开事务内裸 ``return`` 造成事务
        泄漏（本类既有方法以「先判态后写入 + 成功路径末尾 COMMIT」的
        手写模式保持不变；8.5-A 新增方法统一走本 CM）。
        """
        # 不变量与锁冲突翻译统一在 _begin_immediate 内（单点实现）。
        with self._publication_lock:
            self._begin_immediate()
            self._collaboration_notifications.clear()
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException as error:
                self._collaboration_notifications.clear()
                try:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                except BaseException as rollback_error:  # noqa: BLE001 - 回滚失败不能覆盖原异常
                    error.add_note(
                        "session control 写事务回滚失败；原始事务异常仍优先保留: "
                        f"{rollback_error!r}"
                    )
                    try:
                        self.close()
                    except BaseException as close_error:  # noqa: BLE001 - 关闭失败也只作为诊断附加
                        error.add_note(
                            "session control 关闭失败；原始事务异常仍优先保留: "
                            f"{close_error!r}"
                        )
                raise
            revisions = tuple(self._collaboration_notifications)
            self._collaboration_notifications.clear()
            if self._collaboration_publication is not None:
                for revision in revisions:
                    self._collaboration_publication(self.get_collaboration_fanout(revision))

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个只读事务：多语句读取共享同一 SQLite 快照。"""
        with self._publication_lock:
            self._ensure_open()
            if self._connection.in_transaction:
                raise RuntimeError(_store_failure(self.database_path, "BEGIN DEFERRED",
                    "session control 读事务嵌套：read_transaction 不可重入",
                ))
            self._connection.execute("BEGIN DEFERRED")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException as error:
                try:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                except BaseException as rollback_error:  # noqa: BLE001 - 保留原始事务异常。
                    error.add_note(
                        "session control 读事务回滚失败；原始事务异常仍优先保留: "
                        f"{rollback_error!r}"
                    )
                    try:
                        self.close()
                    except BaseException as close_error:  # noqa: BLE001 - 关闭失败只附加诊断。
                        error.add_note(
                            "session control 关闭失败；原始事务异常仍优先保留: "
                            f"{close_error!r}"
                        )
                raise

    def _connect(self) -> None:
        self._connection = sqlite3.connect(
            sqlite_file_uri(self.database_path) + ("?mode=ro" if self._read_only else ""),
            uri=True,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        # 连接创建后立即归构造作用域；配置和建表失败统一由其回收。
        self._closed = False
        connection = self._connection
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in ((self.SCHEMA_VERSION,) if self._read_only else (0, self.SCHEMA_VERSION)):
            raise RuntimeError(_store_failure(self.database_path, "schema_version",
                "session control schema 版本不受支持，fail-closed 拒绝打开（旧生成数据不迁移）: "
                f"user_version={current}, supported={self.SCHEMA_VERSION}",
            ))
        if current == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_schema ORDER BY name LIMIT 1"
            ).fetchone()
            if existing is not None:
                logger.error("session control 非空 v0 schema object=%r; path=%s", existing[0], self.database_path)
                raise RuntimeError(_store_failure(self.database_path, "schema_version",
                    "session control schema 版本不受支持，fail-closed 拒绝非空 v0 库（不迁移或修复）: "
                    f"user_version=0, supported={self.SCHEMA_VERSION}",
                ))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        if self._read_only:
            connection.execute("PRAGMA query_only = ON")
            return
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        if journal_mode.lower() != "wal":
            logger.error("session control WAL 拒绝: journal_mode=%r; path=%s", journal_mode, self.database_path)
            raise RuntimeError(_store_failure(self.database_path, "journal_mode", "session control 无法启用 WAL"))
