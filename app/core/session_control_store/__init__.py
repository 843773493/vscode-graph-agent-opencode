"""per-session ``session-control.sqlite`` 的最小基础设施（facade）。

原先的单文件 ``session_control_store.py`` 已按垂直链路拆入本包：thread creation
record（``thread_creation_record.py``）、创建发布与终结（``thread_creation_
publish.py``）、初始 execution intent（``execution_intent.py``）、collaboration
成员账本（``collaboration.py``）、schema 初始化与版本升级（``_schema.py``），
共用 SQL 常量收敛在 ``sql.py``。

本 facade 只保留 ``SessionControlStore`` 的类声明（四个既有 mixin + 五个新
mixin）与连接生命周期方法（``__init__``/``connection``/``close``/
``_ensure_open``/``_begin_immediate``/``_write_transaction``/``_connect``），
并按原名再导出模块级公开符号，对外契约与导入路径保持不变：
``app.core.session_control_store.SessionControlStore``。

错误分类约定沿用 ``session_catalog_store.py``：``TypeError`` 输入类型错误、
``ValueError`` 输入形态非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义
冲突。连接约定：WAL、``foreign_keys``、``busy_timeout``、``sqlite3.Row``、
``PRAGMA user_version`` 非 0/1/``SCHEMA_VERSION`` 时 fail-closed 拒绝打开。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.session_catalog_store import validate_thread_id as validate_thread_id
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
from app.core.session_control_store.collaboration import (
    CollaborationMember as CollaborationMember,
)
from app.core.session_control_store.collaboration import (
    CollaborationMixin,
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
    validate_sqlite_path_budget,
)

__all__ = [
    "CommunicationInboxRecord",
    "CommunicationOutboxRecord",
    "SessionControlStore",
    "ThreadCreationRecord",
    "ThreadExecutionIntent",
    "ThreadOwnerBinding",
    "compute_initial_execution_binding_preimage_hash",
    "derive_communication_admission_identity",
    "derive_initial_execution_identity",
]


class SessionControlStore(
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

    构造时创建 database_path 父目录并幂等建表；``user_version`` 只允许
    0（新库，初始化后写 5）、1（R12/R13 v1 库，单事务加法升级后写 5）、
    2（R20 v2 库，``thread_execution_intents`` 单事务加法升级补齐稳定
    binding/job identity 后写 5）、3（R23 库，加法补建 collaboration
    ledger/member 表）、4（R25 库）、5（B2：加法补建
    ``session_operation_leases`` 表与非终态索引）、6（B1：加法补建
    ``thread_owner_bindings`` owner 字段槽）或 7（D5：加法补建
    ``communication_outbox``/``communication_inbox`` 通信 ledger 表与
    inbox 状态索引），其余版本
    fail-closed 拒绝打开。
    """

    SCHEMA_VERSION = 7

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError(
                f"database_path 必须是 Path: {database_path!r}"
            )
        self.database_path = database_path.expanduser().resolve()
        validate_sqlite_path_budget(self.database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._connection = self._connect()
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

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
        self._closed = True
        self._connection.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                f"session control store 已关闭: {self.database_path}"
            )

    def _begin_immediate(self) -> None:
        """开启写事务（BEGIN IMMEDIATE），锁冲突转明确领域错误。

        本库只持一条共享连接，但同一 session 库可能被多个进程同时打开
        （resolver / worker / 删除流各持一份）；跨进程写竞争超过
        ``SQLITE_BUSY_TIMEOUT_MS`` 时 sqlite3 抛裸 ``OperationalError:
        database is locked``，该文案不含库路径与等待时长，无法定位。
        此处统一转成含操作阶段、库路径与等待时长的领域错误。

        不变量：不可重入。嵌套调用会在外层事务内再 BEGIN，本方法在
        进入前响亮失败，避免 sqlite3 的 "cannot start a transaction
        within a transaction"（详见 ``_write_transaction``）。
        """
        self._ensure_open()
        if self._connection.in_transaction:
            raise RuntimeError(
                "session control 写事务嵌套：_begin_immediate 不可重入"
                "（外层事务未结束，内层 BEGIN IMMEDIATE 会破坏事务边界）: "
                f"path={self.database_path}"
            )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            waited_ms = int(
                self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            raise RuntimeError(
                "session control 获取写事务失败：另一个进程持有写锁，"
                f"等待 {waited_ms}ms 后仍被占用（fail loud）: "
                f"stage=BEGIN IMMEDIATE, path={self.database_path}, "
                f"error={error}"
            ) from error

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE 内先验证后写入。

        正常退出（含事务体内 ``return``）一律 COMMIT，任何异常 ROLLBACK
        后原样抛出——模式对齐 ``session_catalog_store.write_transaction``，
        吸取 R14 审查 M1 教训：禁止在打开事务内裸 ``return`` 造成事务
        泄漏（本类既有方法以「先判态后写入 + 成功路径末尾 COMMIT」的
        手写模式保持不变；8.5-A 新增方法统一走本 CM）。
        """
        # 不变量与锁冲突翻译统一在 _begin_immediate 内（单点实现）。
        self._begin_immediate()
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                "session control 无法启用 WAL: "
                f"path={self.database_path}, journal_mode={journal_mode}"
            )
        return connection
