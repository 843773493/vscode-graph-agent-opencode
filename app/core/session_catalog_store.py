"""session-catalog.sqlite 基础设施：nodes 表、验证器、gate/fence 原语与 CTE 查询。

本模块实现 workspace 导航 SQLite catalog 的基础设施层。SQLite ``nodes``
表是目录关系、显示名和物理 locator 的唯一权威；物理目录只按 locator
受检解析，不能反向重建或覆盖 catalog。一次性迁移模块可以读取旧 JSON
索引，但生产读写链路不再双读。

``session_creation_records`` 与 ``subtree_delete_records`` 是同一 SQLite
数据库中的操作 journal，用于创建和删除流程的恢复进度，不是第二套目录
权威。打开旧 schema 时只执行已声明的幂等 schema 升级，未知版本直接拒绝。

错误分类约定：

- ``TypeError``：输入类型错误（非字符串 ID/locator、非 datetime 的 created_at）。
- ``ValueError``：输入形态非法（ID/locator/日期/预算）。
- ``KeyError``：目标节点不存在。
- ``RuntimeError``：语义冲突（父节点 deleting、跨 workspace、环、重复 ID、
  非法状态转移、目录一致性校验失败）。
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS

__all__ = [
    "SessionCatalogNode",
    "SessionCatalogStore",
    "SessionCreationRecord",
    "SessionLifecycleFence",
    "SubtreeDeleteRecord",
    "SubtreeFrozenNode",
    "validate_path_budget",
    "validate_session_id",
    "validate_storage_relative_locator",
    "validate_thread_id",
]

# canonical ID profile：前缀 + 32 位小写 hex，恰好 36 个 ASCII byte。
_SESSION_ID_PATTERN = re.compile(r"ses_[0-9a-f]{32}")
_THREAD_ID_PATTERN = re.compile(r"thr_[0-9a-f]{32}")
# storage locator 形态：sessions/YYYY/MM/DD/{session_id}。
_STORAGE_LOCATOR_PATTERN = re.compile(
    r"sessions/([0-9]{4})/([0-9]{2})/([0-9]{2})/(ses_[0-9a-f]{32})"
)

# UUIDv4 位 profile：payload 第 13 个 hex（0 基 index 12）固定为 '4'，
# 第 17 个 hex（index 16）必须属于 variant 一组 '8'|'9'|'a'|'b'。
_UUID_VERSION_HEX_INDEX = 12
_UUID_VARIANT_HEX_INDEX = 16
_UUID_VARIANT_HEX_CHARS = "89ab"

# 路径预算：每组件 ≤255 bytes、总长 ≤4096 bytes（落盘前由 resolver 校验）。
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_PATH_TOTAL_BYTES = 4096

_LOCATOR_PREFIX = "sessions/"

_NODE_COLUMNS = (
    "node_id, kind, parent_node_id, display_name, state, revision, "
    "workspace_id, created_at, storage_relative_locator, main_thread_id"
)

_NODES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'session')),
    parent_node_id TEXT REFERENCES nodes(node_id),
    display_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    revision INTEGER NOT NULL DEFAULT 1,
    workspace_id TEXT NOT NULL,
    created_at TEXT,
    storage_relative_locator TEXT,
    main_thread_id TEXT,
    CHECK (
        (kind = 'session'
            AND created_at IS NOT NULL
            AND storage_relative_locator IS NOT NULL
            AND main_thread_id IS NOT NULL)
        OR (kind = 'folder'
            AND created_at IS NULL
            AND storage_relative_locator IS NULL
            AND main_thread_id IS NULL)
    ),
    UNIQUE (workspace_id, main_thread_id),
    UNIQUE (workspace_id, storage_relative_locator)
)
"""

_IDX_NODES_PARENT_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_node_id)"
)
_IDX_NODES_WORKSPACE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_workspace ON nodes(workspace_id)"
)

# SessionCreationRecord journal 表（8.1-A）：与导航 node 同库、不是第二权威。
# record 冻结 Session 身份/locator/preimage/父节点 revision；publish 是唯一
# 可见性提交点（node 行在本表同事务内插入）。
_CREATION_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS session_creation_records (
    session_creation_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    main_thread_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    parent_node_id TEXT,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    storage_relative_locator TEXT NOT NULL UNIQUE,
    preimage_hash TEXT NOT NULL,
    parent_revision INTEGER,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
"""

_IDX_CREATION_RECORDS_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_creation_records_state "
    "ON session_creation_records(state)"
)

_CREATION_RECORD_COLUMNS = (
    "session_creation_idempotency_key, session_id, main_thread_id, workspace_id, "
    "parent_node_id, display_name, created_at, storage_relative_locator, "
    "preimage_hash, parent_revision, state, abort_reason, "
    "record_created_at, record_updated_at"
)

# NavigationSubtreeDeleteRecord journal 表（8.1-B）：与导航 node 同库、不是
# 第二权威。record 冻结递归 CTE 得到的精确子树 (node_id, revision) 集合与
# 每个 session 的不可变 locator；mark 的单事务 CAS 整树 active→deleting 是
# 唯一逻辑可见性关闭点；``drained_session_ids`` 追加已物理隔离 session
# （JSON 数组，恢复时按此定点继续）。R14 起本表随 ``_initialize`` 加法补建
# （``CREATE TABLE IF NOT EXISTS`` 对既有 v2 库幂等，不改 user_version）。
_SUBTREE_DELETE_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS subtree_delete_records (
    subtree_delete_idempotency_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    root_node_id TEXT NOT NULL,
    frozen_node_ids TEXT NOT NULL,
    frozen_session_locators TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'deleting', 'draining', 'completed', 'aborted')),
    abort_reason TEXT,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL,
    drained_session_ids TEXT NOT NULL DEFAULT '[]'
)
"""

_IDX_SUBTREE_DELETE_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_subtree_delete_state "
    "ON subtree_delete_records(state)"
)

# user_version 与「必须已存在」的表绑定：已登记应用的版本若缺表，说明库被
# 外部进程改写（或被截断）。此时下面的 ``CREATE TABLE IF NOT EXISTS`` 会把
# 权威表静默重建为**空表**，等于把全部会话位置与父子关系悄悄丢光，因此必须
# 在写任何 DDL 之前 fail closed。``subtree_delete_records`` 是 R14 加法补表
# （``user_version`` 保持 2），不属于任何版本的硬性要求，故不在绑定表中。
_REQUIRED_TABLES_BY_VERSION = {
    1: ("nodes",),
    2: ("nodes", "session_creation_records"),
}

_SUBTREE_DELETE_RECORD_COLUMNS = (
    "subtree_delete_idempotency_key, workspace_id, root_node_id, "
    "frozen_node_ids, frozen_session_locators, state, abort_reason, "
    "record_created_at, record_updated_at, drained_session_ids"
)


def validate_session_id(value: str) -> None:
    """校验 canonical session_id：``ses_`` 前缀 + 32 位小写 hex + UUIDv4 位 profile。

    斜杠、反斜杠、Unicode、``.``/``..``、大小写错误、前缀错误、长度错误及
    非 v4 位 profile 一律直接拒绝，不得清洗或截断。
    """
    if not isinstance(value, str):
        raise TypeError(f"session_id 必须是字符串: {value!r}")
    if _SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"session_id 形态非法: {value!r}")
    _validate_uuid_v4_payload(value[4:])


def validate_thread_id(value: str) -> None:
    """校验 canonical thread_id：``thr_`` 前缀 + 32 位小写 hex + UUIDv4 位 profile。"""
    if not isinstance(value, str):
        raise TypeError(f"thread_id 必须是字符串: {value!r}")
    if _THREAD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"thread_id 形态非法: {value!r}")
    _validate_uuid_v4_payload(value[4:])


def _validate_uuid_v4_payload(payload: str) -> None:
    """校验 32 位 hex payload 的 UUIDv4 version/variant 位。"""
    if payload[_UUID_VERSION_HEX_INDEX] != "4":
        raise ValueError(f"ID payload 的 UUID version 位非法: {payload!r}")
    if payload[_UUID_VARIANT_HEX_INDEX] not in _UUID_VARIANT_HEX_CHARS:
        raise ValueError(f"ID payload 的 UUID variant 位非法: {payload!r}")


def validate_storage_relative_locator(value: str) -> None:
    """校验 storage locator：``sessions/YYYY/MM/DD/{session_id}`` 且日期真实存在。

    YYYY 为 4 位数字，MM 必须在 01-12，DD 按 ``calendar.monthrange`` 对应
    月份合法（含闰年）；叶名 session_id 必须过完整 session 验证器。
    """
    if not isinstance(value, str):
        raise TypeError(f"storage_relative_locator 必须是字符串: {value!r}")
    match = _STORAGE_LOCATOR_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"storage_relative_locator 形态非法: {value!r}")
    year_text, month_text, day_text, session_id = match.groups()
    validate_session_id(session_id)
    month = int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"storage_relative_locator 月份非法: {value!r}")
    day = int(day_text)
    _, last_day = calendar.monthrange(int(year_text), month)
    if not 1 <= day <= last_day:
        raise ValueError(f"storage_relative_locator 日期非法: {value!r}")


def validate_path_budget(base: Path, locator: str) -> None:
    """校验解析后绝对路径的组件与总长预算。

    每个路径组件不超过 255 bytes，完整路径不超过 4096 bytes；超限直接
    拒绝，不截断、不改写叶名或以 path hash 替代真实叶名。
    """
    target = (base / locator).resolve()
    for part in target.parts:
        component_bytes = len(part.encode("utf-8"))
        if component_bytes > _MAX_PATH_COMPONENT_BYTES:
            raise ValueError(
                f"路径组件超出预算: {part!r} "
                f"({component_bytes} bytes > {_MAX_PATH_COMPONENT_BYTES})"
            )
    total_bytes = len(str(target).encode("utf-8"))
    if total_bytes > _MAX_PATH_TOTAL_BYTES:
        raise ValueError(
            f"路径总长超出预算: {target} "
            f"({total_bytes} bytes > {_MAX_PATH_TOTAL_BYTES})"
        )


class SessionLifecycleFence:
    """单 session 生命周期栅栏：state + generation 的 CAS 原语。

    ``active → deleting`` 是唯一合法转移，成功时 generation+1；
    ``deleting → active`` 恒拒绝（不可复活）。
    """

    def __init__(self, state: str = "active", generation: int = 0) -> None:
        if state not in ("active", "deleting"):
            raise ValueError(f"fence 初始状态非法: {state!r}")
        self.state = state
        self.generation = generation

    def cas_transition(self, expected_generation: int, new_state: str) -> bool:
        """generation 匹配且转移合法时推进状态并返回 True，否则返回 False。"""
        if new_state not in ("active", "deleting"):
            raise ValueError(f"fence 目标状态非法: {new_state!r}")
        if self.state != "active" or new_state != "deleting":
            return False
        if self.generation != expected_generation:
            return False
        self.generation += 1
        self.state = new_state
        return True


@dataclass(frozen=True, slots=True)
class SessionCatalogNode:
    """nodes 表行的不可变投影。"""

    node_id: str
    kind: str
    parent_node_id: str | None
    display_name: str
    state: str
    revision: int
    workspace_id: str
    created_at: str | None
    storage_relative_locator: str | None
    main_thread_id: str | None


@dataclass(frozen=True, slots=True)
class SessionCreationRecord:
    """session_creation_records 表行的不可变投影（8.1-A 创建流 journal）。

    ``state`` 闭集为 ``preparing/published/aborted``；``parent_revision``
    是 record 建立时冻结的父节点 revision（publish 时 CAS 校验，漂移即
    失败）；``created_at`` 是冻结的 Session UTC 创建时刻（ISO 文本），与
    ``record_created_at/record_updated_at``（journal 自身记账时刻）不同。
    """

    session_creation_idempotency_key: str
    session_id: str
    main_thread_id: str
    workspace_id: str
    parent_node_id: str | None
    display_name: str
    created_at: str
    storage_relative_locator: str
    preimage_hash: str
    parent_revision: int | None
    state: str
    abort_reason: str | None
    record_created_at: str
    record_updated_at: str


@dataclass(frozen=True, slots=True)
class SubtreeFrozenNode:
    """``subtree_delete_records.frozen_node_ids`` JSON 数组元素的强类型投影。

    ``revision`` 是 record 建立时冻结的节点 revision（mark CAS 校验漂移的
    期望值来源，对齐 design.md「冻结……父子revision」）。
    """

    node_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class SubtreeDeleteRecord:
    """subtree_delete_records 表行的不可变投影（8.1-B 子树删除流 journal）。

    ``state`` 闭集为 ``preparing/deleting/draining/completed/aborted``；
    ``frozen_node_ids`` 是递归 CTE 冻结的 (node_id, revision) 集合（含
    root，按 node_id 排序）；``frozen_session_locators`` 是
    session_id → 不可变 storage locator 映射（恢复时按此定点继续，不重查
    当前树）；``drained_session_ids`` 是已物理隔离 session 的追加序数组。
    """

    subtree_delete_idempotency_key: str
    workspace_id: str
    root_node_id: str
    frozen_node_ids: tuple[SubtreeFrozenNode, ...]
    frozen_session_locators: dict[str, str]
    state: str
    abort_reason: str | None
    record_created_at: str
    record_updated_at: str
    drained_session_ids: tuple[str, ...]


def _validate_workspace_id(workspace_id: object) -> None:
    """校验 workspace_id：非空字符串。"""
    if not isinstance(workspace_id, str):
        raise TypeError(f"workspace_id 必须是字符串: {workspace_id!r}")
    if not workspace_id:
        raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")


def _parse_frozen_node_ids(raw: str) -> tuple[SubtreeFrozenNode, ...]:
    """解析 frozen_node_ids JSON 数组；结构非法即 fail closed（外部改动）。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record frozen_node_ids 无法解析（record 被外部改动，"
            f"fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record frozen_node_ids 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    items: list[SubtreeFrozenNode] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"node_id", "revision"}:
            raise RuntimeError(
                "subtree delete record frozen_node_ids 元素结构非法"
                f"（record 被外部改动，fail closed）: {entry!r}"
            )
        node_id = entry["node_id"]
        revision = entry["revision"]
        try:
            validate_session_id(node_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record 冻结 node_id 非法"
                f"（record 被外部改动，fail closed）: {node_id!r}: {error}"
            ) from error
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise RuntimeError(
                "subtree delete record 冻结 revision 非法"
                f"（record 被外部改动，fail closed）: {revision!r}"
            )
        if node_id in seen:
            raise RuntimeError(
                "subtree delete record 冻结集合包含重复 node_id"
                f"（record 被外部改动，fail closed）: {node_id!r}"
            )
        seen.add(node_id)
        items.append(SubtreeFrozenNode(node_id=node_id, revision=revision))
    return tuple(items)


def _parse_frozen_session_locators(raw: str) -> dict[str, str]:
    """解析 frozen_session_locators JSON 数组；结构非法即 fail closed。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record frozen_session_locators 无法解析"
            f"（record 被外部改动，fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record frozen_session_locators 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    locators: dict[str, str] = {}
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {
            "session_id",
            "storage_relative_locator",
        }:
            raise RuntimeError(
                "subtree delete record frozen_session_locators 元素结构非法"
                f"（record 被外部改动，fail closed）: {entry!r}"
            )
        session_id = entry["session_id"]
        locator = entry["storage_relative_locator"]
        try:
            validate_session_id(session_id)
            validate_storage_relative_locator(locator)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record 冻结 session locator 非法"
                f"（record 被外部改动，fail closed）: {entry!r}: {error}"
            ) from error
        if session_id in locators:
            raise RuntimeError(
                "subtree delete record 冻结 locator 集合包含重复 session_id"
                f"（record 被外部改动，fail closed）: {session_id!r}"
            )
        locators[session_id] = locator
    return locators


def _parse_drained_session_ids(raw: str) -> tuple[str, ...]:
    """解析 drained_session_ids JSON 数组；结构非法即 fail closed。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record drained_session_ids 无法解析"
            f"（record 被外部改动，fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record drained_session_ids 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    seen: set[str] = set()
    for session_id in payload:
        try:
            validate_session_id(session_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record drained session_id 非法"
                f"（record 被外部改动，fail closed）: {session_id!r}: {error}"
            ) from error
        if session_id in seen:
            raise RuntimeError(
                "subtree delete record drained_session_ids 包含重复 session_id"
                f"（record 被外部改动，fail closed）: {session_id!r}"
            )
        seen.add(session_id)
    return tuple(str(session_id) for session_id in payload)


class SessionCatalogStore:
    """session-catalog.sqlite 的 nodes 表与 creation record journal 基础设施。

    SQLite ``nodes`` 表是唯一目录权威。构造时创建 database_path 的父目录，
    但**不创建** sessions_root 目录（store 只管 catalog，不管物理树）。
    创建和删除 journal 与 nodes 同库，由事务保证目录可见性和恢复进度的一致性。
    """

    SCHEMA_VERSION = 2

    def __init__(self, database_path: Path, sessions_root: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.sessions_root = sessions_root.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        # R18-3：单连接跨线程串行锁（可重入）。本 store 只持有一条共享
        # sqlite3 连接（check_same_thread=False），历史实现无任何锁——跨
        # 线程并发进入 _read_transaction/_write_transaction 会在同一连接
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
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                "session catalog 无法启用 WAL: "
                f"path={self.database_path}, journal_mode={journal_mode}"
            )
        return connection

    def _initialize(self) -> None:
        """幂等建表并设置 user_version；未知版本 fail-closed 拒绝打开。

        支持的 current：0（全新库，建全部表并置 v2）、1（R10 v1 库，
        同一事务内加法建 ``session_creation_records`` 表并升 v2，不动
        nodes 表）、2（已是当前版本；R14 起以 ``CREATE TABLE IF NOT
        EXISTS`` 幂等补建 ``subtree_delete_records`` 表——加法式补表，
        不改 user_version，既有表 DDL 与数据不受影响）。
        """
        current = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current not in (0, 1, self.SCHEMA_VERSION):
            raise RuntimeError(
                "session catalog schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        if current:
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
            if current != self.SCHEMA_VERSION:
                self._connection.execute(
                    f"PRAGMA user_version = {self.SCHEMA_VERSION}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

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
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE 内先验证后写入，异常回滚。

        R18-3：事务全程（BEGIN → yield 出的语句执行窗口 → COMMIT/
        ROLLBACK）持有连接串行锁——跨线程的后到事务阻塞至先到事务结束，
        共享连接上不再出现事务交叠（OperationalError 两种变体的根源）。
        SQL 语义与 BEGIN 模式不变；RLock 可重入，同线程嵌套安全。
        """
        with self._connection_lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个读事务：多语句读共享同一快照。

        R18-3：事务全程持有连接串行锁（语义同 ``_write_transaction``），
        跨线程读/写/读在共享连接上完全串行，不再交叠。
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

    @staticmethod
    def _fetch_node(
        connection: sqlite3.Connection,
        node_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()

    def _require_node(
        self,
        connection: sqlite3.Connection,
        node_id: str,
    ) -> sqlite3.Row:
        row = self._fetch_node(connection, node_id)
        if row is None:
            raise KeyError(f"会话目录节点不存在: {node_id}")
        return row

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> SessionCatalogNode:
        return SessionCatalogNode(
            node_id=str(row["node_id"]),
            kind=str(row["kind"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            display_name=str(row["display_name"]),
            state=str(row["state"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            created_at=(
                str(row["created_at"]) if row["created_at"] is not None else None
            ),
            storage_relative_locator=(
                str(row["storage_relative_locator"])
                if row["storage_relative_locator"] is not None
                else None
            ),
            main_thread_id=(
                str(row["main_thread_id"])
                if row["main_thread_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _validate_common_fields(workspace_id: str, display_name: str) -> None:
        if not isinstance(workspace_id, str):
            raise TypeError(f"workspace_id 必须是字符串: {workspace_id!r}")
        if not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        if not isinstance(display_name, str):
            raise TypeError(f"显示名必须是字符串: {display_name!r}")
        if not display_name:
            raise ValueError(f"显示名不能为空: {display_name!r}")

    def _require_node_id_available(
        self,
        connection: sqlite3.Connection,
        node_id: str,
    ) -> None:
        if self._fetch_node(connection, node_id) is not None:
            raise RuntimeError(f"节点 ID 已存在: {node_id}")

    def _require_mutable_parent(
        self,
        connection: sqlite3.Connection,
        parent_node_id: str | None,
        workspace_id: str,
    ) -> None:
        """验证父节点存在、非 deleting 且与子节点同 workspace。"""
        if parent_node_id is None:
            return
        parent = self._require_node(connection, parent_node_id)
        if parent["state"] == "deleting":
            raise RuntimeError(f"父节点正在删除，拒绝挂载: {parent_node_id}")
        if parent["workspace_id"] != workspace_id:
            raise RuntimeError(
                "父节点属于其他 workspace，拒绝跨 workspace 挂载: "
                f"parent={parent_node_id}, "
                f"parent_workspace={parent['workspace_id']}, "
                f"child_workspace={workspace_id}"
            )

    @staticmethod
    def _validate_locator_matches_session(
        node_id: str,
        created_at: datetime,
        storage_relative_locator: str,
    ) -> None:
        """验证 locator 叶名等于 session_id，且日期等于 created_at 的 UTC 日期。"""
        parts = storage_relative_locator.split("/")
        locator_session_id = parts[4]
        if locator_session_id != node_id:
            raise ValueError(
                "locator 叶名必须等于 session_id: "
                f"locator={storage_relative_locator}, session_id={node_id}"
            )
        locator_date = date(int(parts[1]), int(parts[2]), int(parts[3]))
        created_date = created_at.astimezone(UTC).date()
        if locator_date != created_date:
            raise ValueError(
                "locator 日期必须等于 created_at 的 UTC 日期: "
                f"locator={storage_relative_locator}, "
                f"created_at={created_at.isoformat()}, "
                f"utc_date={created_date.isoformat()}"
            )

    def _validate_locator_budget(self, storage_relative_locator: str) -> None:
        """locator 去 ``sessions/`` 前缀后对 sessions_root 做路径预算校验。"""
        relative = storage_relative_locator[len(_LOCATOR_PREFIX):]
        validate_path_budget(self.sessions_root, relative)

    def _require_unique_session_fields(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        storage_relative_locator: str,
        main_thread_id: str,
    ) -> None:
        duplicate_thread = connection.execute(
            "SELECT node_id FROM nodes "
            "WHERE workspace_id = ? AND main_thread_id = ?",
            (workspace_id, main_thread_id),
        ).fetchone()
        if duplicate_thread is not None:
            raise RuntimeError(
                "main_thread_id 已被同 workspace 的 session 占用: "
                f"workspace_id={workspace_id}, main_thread_id={main_thread_id}, "
                f"existing_node={duplicate_thread[0]}"
            )
        duplicate_locator = connection.execute(
            "SELECT node_id FROM nodes "
            "WHERE workspace_id = ? AND storage_relative_locator = ?",
            (workspace_id, storage_relative_locator),
        ).fetchone()
        if duplicate_locator is not None:
            raise RuntimeError(
                "storage_relative_locator 已被同 workspace 的 session 占用: "
                f"workspace_id={workspace_id}, "
                f"locator={storage_relative_locator}, "
                f"existing_node={duplicate_locator[0]}"
            )

    @staticmethod
    def _descendant_node_ids(
        connection: sqlite3.Connection,
        node_id: str,
    ) -> set[str]:
        """递归 CTE 求全部后代节点 ID（含 folder 与 session，不含自身）。"""
        rows = connection.execute(
            """
            WITH RECURSIVE descendants(node_id) AS (
                SELECT node_id FROM nodes WHERE parent_node_id = ?
                UNION
                SELECT n.node_id FROM nodes n
                JOIN descendants d ON n.parent_node_id = d.node_id
            )
            SELECT node_id FROM descendants
            """,
            (node_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    # ------------------------------------------------------------------
    # 写操作（每个一事务，事务内先验证后写入）
    # ------------------------------------------------------------------

    def create_folder(
        self,
        node_id: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
    ) -> SessionCatalogNode:
        """创建 folder 节点；folder 无物理 locator/manifest。

        folder 节点 ID 使用与 session 相同的 canonical ``ses_`` 前缀形态，
        但 folder 本身不分配物理 locator。
        """
        validate_session_id(node_id)
        self._validate_common_fields(workspace_id, display_name)
        with self._write_transaction() as connection:
            self._require_node_id_available(connection, node_id)
            self._require_mutable_parent(connection, parent_node_id, workspace_id)
            connection.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "state, revision, workspace_id) "
                "VALUES (?, 'folder', ?, ?, 'active', 1, ?)",
                (node_id, parent_node_id, display_name, workspace_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def create_session_node(
        self,
        node_id: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        created_at: datetime,
        storage_relative_locator: str,
        main_thread_id: str,
    ) -> SessionCatalogNode:
        """创建 session 节点；locator 日期必须等于 created_at 的 UTC 日期。"""
        validate_session_id(node_id)
        validate_thread_id(main_thread_id)
        validate_storage_relative_locator(storage_relative_locator)
        self._validate_common_fields(workspace_id, display_name)
        if not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        self._validate_locator_matches_session(
            node_id,
            created_at,
            storage_relative_locator,
        )
        self._validate_locator_budget(storage_relative_locator)
        with self._write_transaction() as connection:
            self._require_node_id_available(connection, node_id)
            self._require_unique_session_fields(
                connection,
                workspace_id,
                storage_relative_locator,
                main_thread_id,
            )
            self._require_mutable_parent(connection, parent_node_id, workspace_id)
            connection.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "state, revision, workspace_id, created_at, "
                "storage_relative_locator, main_thread_id) "
                "VALUES (?, 'session', ?, ?, 'active', 1, ?, ?, ?, ?)",
                (
                    node_id,
                    parent_node_id,
                    display_name,
                    workspace_id,
                    created_at.isoformat(),
                    storage_relative_locator,
                    main_thread_id,
                ),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def rename_node(self, node_id: str, display_name: str) -> SessionCatalogNode:
        """重命名节点；显示名只存 catalog，不参与物理路径。"""
        if not isinstance(display_name, str):
            raise TypeError(f"显示名必须是字符串: {display_name!r}")
        if not display_name:
            raise ValueError(f"显示名不能为空: {display_name!r}")
        with self._write_transaction() as connection:
            self._require_node(connection, node_id)
            connection.execute(
                "UPDATE nodes SET display_name = ?, revision = revision + 1 "
                "WHERE node_id = ?",
                (display_name, node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def move_node(
        self,
        node_id: str,
        new_parent_node_id: str | None,
    ) -> SessionCatalogNode:
        """调整父节点；只改导航关系，不搬移物理目录。"""
        with self._write_transaction() as connection:
            node = self._require_node(connection, node_id)
            if new_parent_node_id == node_id:
                raise RuntimeError(f"移动目标不能是节点自身: {node_id}")
            if new_parent_node_id is not None:
                parent = self._require_node(connection, new_parent_node_id)
                if parent["state"] == "deleting":
                    raise RuntimeError(
                        f"目标父节点正在删除: {new_parent_node_id}"
                    )
                if parent["workspace_id"] != node["workspace_id"]:
                    raise RuntimeError(
                        "目标父节点属于其他 workspace: "
                        f"node={node_id}, node_workspace={node['workspace_id']}, "
                        f"parent={new_parent_node_id}, "
                        f"parent_workspace={parent['workspace_id']}"
                    )
                if new_parent_node_id in self._descendant_node_ids(
                    connection,
                    node_id,
                ):
                    raise RuntimeError(
                        "移动会形成循环: "
                        f"node={node_id}, new_parent={new_parent_node_id}"
                    )
            connection.execute(
                "UPDATE nodes SET parent_node_id = ?, revision = revision + 1 "
                "WHERE node_id = ?",
                (new_parent_node_id, node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def set_node_state(self, node_id: str, state: str) -> SessionCatalogNode:
        """设置节点状态；active→deleting 允许，deleting→active 拒绝（不可复活）。"""
        if state not in ("active", "deleting"):
            raise ValueError(f"节点状态非法: {state!r}")
        with self._write_transaction() as connection:
            row = self._require_node(connection, node_id)
            current = str(row["state"])
            if current == state:
                raise RuntimeError(
                    f"节点状态已是 {state}，拒绝无变化写入: {node_id}"
                )
            if current == "deleting":
                raise RuntimeError(f"节点正在删除，不可复活: {node_id}")
            connection.execute(
                "UPDATE nodes SET state = ?, revision = revision + 1 "
                "WHERE node_id = ?",
                (state, node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    # ------------------------------------------------------------------
    # SessionCreationRecord journal（8.1-A，R13 加法扩展）
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_creation_record(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
            "WHERE session_creation_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _creation_record_from_row(row: sqlite3.Row) -> SessionCreationRecord:
        return SessionCreationRecord(
            session_creation_idempotency_key=str(
                row["session_creation_idempotency_key"]
            ),
            session_id=str(row["session_id"]),
            main_thread_id=str(row["main_thread_id"]),
            workspace_id=str(row["workspace_id"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            display_name=str(row["display_name"]),
            created_at=str(row["created_at"]),
            storage_relative_locator=str(row["storage_relative_locator"]),
            preimage_hash=str(row["preimage_hash"]),
            parent_revision=(
                int(row["parent_revision"])
                if row["parent_revision"] is not None
                else None
            ),
            state=str(row["state"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str):
            raise TypeError(
                f"idempotency_key 必须是字符串: {idempotency_key!r}"
            )
        if not idempotency_key:
            raise ValueError("idempotency_key 不能为空")

    def create_or_get_creation_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        created_at: datetime,
        preimage_hash: str,
        session_id: str | None = None,
    ) -> SessionCreationRecord:
        """create-or-get 创建流 journal record（gate 内短事务，8.1-A）。

        - 同 key 已存在：``preimage_hash`` 一致 → 返回既有 record（幂等，
          created_at 等冻结值以既有 record 为准）；不一致 → ``RuntimeError``
          （同 key 不同 preimage 冲突）。
        - ``session_id`` 可选传入已验证的 canonical session ID，仅供固定
          ID 的测试 fixture 使用；生产创建服务始终由 store 分配 ID。
          提供时先过 ``validate_session_id``（非法形态直接 ``ValueError``，
          不清洗不改写），并以该 ID 作为分配结果（不再软件分配）；未提供
          时保持软件分配现状。幂等语义不变：同 key 同 preimage 返回既有
          record——既有 record 的 ``session_id`` 若与传入不同，属同 key
          身份冲突，``RuntimeError`` 拒绝（不静默改绑）。
        - 不存在 → 软件分配（或采用传入的）canonical ``session_id``
          （``ses_``）与软件分配 ``main_thread_id``（``thr_``）、按
          created_at 的 UTC 日期冻结 ``sessions/YYYY/MM/DD/{session_id}``
          最终 locator、冻结父节点当前 revision，插入 ``state='preparing'``。
          **不发布可见 node**。
        - parent 校验：存在、非 deleting、同 workspace（复用
          :meth:`_require_mutable_parent`）；同时预检 session_id /
          main_thread_id / locator 未被既有 nodes 行占用（fail fast，
          UNIQUE 约束兜底）。
        """
        self._validate_idempotency_key(idempotency_key)
        self._validate_common_fields(workspace_id, display_name)
        if not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        if not isinstance(preimage_hash, str) or not preimage_hash:
            raise ValueError(f"preimage_hash 不能为空: {preimage_hash!r}")
        # 传入 session_id 的形态校验先行（在任何事务/状态变更之前 fail fast；
        # 非法形态直接 ValueError，不静默回退到软件分配）。
        if session_id is not None:
            validate_session_id(session_id)
        with self._write_transaction() as connection:
            existing = self._fetch_creation_record(connection, idempotency_key)
            if existing is not None:
                if str(existing["preimage_hash"]) != preimage_hash:
                    raise RuntimeError(
                        "session creation record preimage 冲突（同 key 不同 "
                        "preimage，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={existing['preimage_hash']!r}, "
                        f"requested_preimage={preimage_hash!r}"
                    )
                if (
                    session_id is not None
                    and str(existing["session_id"]) != session_id
                ):
                    raise RuntimeError(
                        "session creation record session_id 冲突（同 key 幂等"
                        "复用时传入 ID 与既有 record 不一致，拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_session_id={existing['session_id']!r}, "
                        f"requested_session_id={session_id!r}"
                    )
                return self._creation_record_from_row(existing)
            # 插入路径：事务内先验证后写入。
            self._require_mutable_parent(connection, parent_node_id, workspace_id)
            parent_revision: int | None = None
            if parent_node_id is not None:
                parent_row = self._fetch_node(connection, parent_node_id)
                if parent_row is None:
                    # 防御性兜底：_require_mutable_parent 刚验证过存在。
                    raise KeyError(f"会话目录节点不存在: {parent_node_id}")
                parent_revision = int(parent_row["revision"])
            # TODO(identifier): "thr" 前缀待 8.5 thread catalog 落地时补入
            # IdentifierPrefix Literal（对齐 session_catalog_migration 的
            # 同款 TODO）；create_prefixed_id 基于 uuid4().hex，天然满足
            # v4 位 profile。
            allocated_session_id = (
                session_id
                if session_id is not None
                else create_prefixed_id("ses")
            )
            main_thread_id = create_prefixed_id("thr")
            validate_session_id(allocated_session_id)
            validate_thread_id(main_thread_id)
            utc_date = created_at.astimezone(UTC).date()
            locator = f"sessions/{utc_date:%Y/%m/%d}/{allocated_session_id}"
            validate_storage_relative_locator(locator)
            self._validate_locator_budget(locator)
            # fail fast：新分配身份不得与既有可见 node 冲突（UNIQUE 兜底）。
            self._require_node_id_available(connection, allocated_session_id)
            self._require_unique_session_fields(
                connection, workspace_id, locator, main_thread_id
            )
            record_created_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO session_creation_records ("
                "session_creation_idempotency_key, session_id, main_thread_id, "
                "workspace_id, parent_node_id, display_name, created_at, "
                "storage_relative_locator, preimage_hash, parent_revision, "
                "state, abort_reason, record_created_at, record_updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', NULL, ?, ?)",
                (
                    idempotency_key,
                    allocated_session_id,
                    main_thread_id,
                    workspace_id,
                    parent_node_id,
                    display_name,
                    created_at.isoformat(),
                    locator,
                    preimage_hash,
                    parent_revision,
                    record_created_at,
                    record_created_at,
                ),
            )
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "creation record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def get_creation_record(self, idempotency_key: str) -> SessionCreationRecord:
        """按幂等键返回 creation record 投影；不存在抛 KeyError。"""
        self._validate_idempotency_key(idempotency_key)
        with self._read_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def publish_creation_record(self, idempotency_key: str) -> SessionCatalogNode:
        """CAS 发布 creation record 并插入可见 node（**唯一可见性提交点**）。

        单事务内依次验证：record 存在且 ``state='preparing'``；record 内部
        一致性（ID/locator 形态、locator 叶名与日期）；父节点仍存在、
        active 且 revision 等于冻结 ``parent_revision``（漂移/缺失/删除 →
        RuntimeError，含期望/实际）；目标 session_id / main_thread_id /
        locator 未被其它 nodes 行占用（显式预检 + UNIQUE 兜底）。随后同一
        事务内插入 nodes 行（等价 create_session_node 语义，身份字段全部
        来自 record 冻结值）并把 record 推进为 ``published``。任何失败回滚
        整个事务：node 不发布、record 保持 preparing。
        """
        self._validate_idempotency_key(idempotency_key)
        with self._write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，拒绝重复发布: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，拒绝发布: "
                    f"key={idempotency_key!r}, "
                    f"abort_reason={row['abort_reason']!r}"
                )
            session_id = str(row["session_id"])
            main_thread_id = str(row["main_thread_id"])
            workspace_id = str(row["workspace_id"])
            parent_node_id = (
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            )
            display_name = str(row["display_name"])
            created_at_text = str(row["created_at"])
            locator = str(row["storage_relative_locator"])
            try:
                created_at = datetime.fromisoformat(created_at_text)
            except ValueError as error:
                raise RuntimeError(
                    "creation record created_at 无法解析（record 被外部改动，"
                    f"fail closed）: {created_at_text!r}: {error}"
                ) from error
            # record 内部一致性复验（防绕过软件直改 record）。
            validate_session_id(session_id)
            validate_thread_id(main_thread_id)
            validate_storage_relative_locator(locator)
            self._validate_locator_matches_session(session_id, created_at, locator)
            self._validate_locator_budget(locator)
            # 父节点 CAS：仍存在、active、revision 未漂移。
            if parent_node_id is not None:
                parent = self._fetch_node(connection, parent_node_id)
                if parent is None:
                    raise RuntimeError(
                        "session creation publish 失败：父节点已不存在"
                        f"（operation 须 abort 后换新 key 重试）: "
                        f"key={idempotency_key!r}, parent={parent_node_id}"
                    )
                if str(parent["state"]) != "active":
                    raise RuntimeError(
                        "session creation publish 失败：父节点非 active: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"parent_state={parent['state']!r}"
                    )
                frozen_revision = row["parent_revision"]
                if frozen_revision is None:
                    raise RuntimeError(
                        "creation record 冻结 parent_revision 缺失（record 被"
                        f"外部改动，fail closed）: key={idempotency_key!r}"
                    )
                actual_revision = int(parent["revision"])
                if actual_revision != int(frozen_revision):
                    raise RuntimeError(
                        "session creation publish 失败：父节点 revision 已漂移: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"expected_revision={int(frozen_revision)}, "
                        f"actual_revision={actual_revision}"
                    )
            # 目标占用预检（UNIQUE 兜底）。
            self._require_node_id_available(connection, session_id)
            self._require_unique_session_fields(
                connection, workspace_id, locator, main_thread_id
            )
            connection.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "state, revision, workspace_id, created_at, "
                "storage_relative_locator, main_thread_id) "
                "VALUES (?, 'session', ?, ?, 'active', 1, ?, ?, ?, ?)",
                (
                    session_id,
                    parent_node_id,
                    display_name,
                    workspace_id,
                    created_at.isoformat(),
                    locator,
                    main_thread_id,
                ),
            )
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            return self._node_from_row(self._require_node(connection, session_id))

    def abort_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SessionCreationRecord:
        """终结 creation record：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。
        """
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self._write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._creation_record_from_row(row)
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_creation_record(connection, idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(updated)

    # ------------------------------------------------------------------
    # NavigationSubtreeDeleteRecord journal（8.1-B，R14 加法扩展）
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_subtree_delete_record(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_SUBTREE_DELETE_RECORD_COLUMNS} FROM subtree_delete_records "
            "WHERE subtree_delete_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _subtree_delete_record_from_row(
        row: sqlite3.Row,
    ) -> SubtreeDeleteRecord:
        return SubtreeDeleteRecord(
            subtree_delete_idempotency_key=str(
                row["subtree_delete_idempotency_key"]
            ),
            workspace_id=str(row["workspace_id"]),
            root_node_id=str(row["root_node_id"]),
            frozen_node_ids=_parse_frozen_node_ids(str(row["frozen_node_ids"])),
            frozen_session_locators=_parse_frozen_session_locators(
                str(row["frozen_session_locators"])
            ),
            state=str(row["state"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
            drained_session_ids=_parse_drained_session_ids(
                str(row["drained_session_ids"])
            ),
        )

    @staticmethod
    def _subtree_rows(
        connection: sqlite3.Connection,
        root_node_id: str,
    ) -> list[sqlite3.Row]:
        """递归 CTE 返回子树全部节点行（含 root 自身，按 node_id 排序）。"""
        return connection.execute(
            """
            WITH RECURSIVE subtree(node_id) AS (
                SELECT node_id FROM nodes WHERE node_id = ?
                UNION
                SELECT n.node_id FROM nodes n
                JOIN subtree s ON n.parent_node_id = s.node_id
            )
            SELECT node_id, kind, parent_node_id, state, revision,
                   workspace_id, storage_relative_locator
            FROM nodes
            WHERE node_id IN (SELECT node_id FROM subtree)
            ORDER BY node_id
            """,
            (root_node_id,),
        ).fetchall()

    def create_or_get_subtree_delete_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        root_node_id: str,
    ) -> SubtreeDeleteRecord:
        """create-or-get 子树删除流 journal record（gate 内短事务，8.1-B）。

        - 同 key 已存在：preimage（``workspace_id`` + ``root_node_id``）
          一致 → 幂等返回既有 record——冻结集合以既有 record 为准，**不
          重查当前树**（对齐 design.md「按 record 定点继续，不重新查询
          当前树」；completed/aborted/deleting 状态下节点行可能已删除或
          已 deleting，重验会破坏恢复重入）；不一致 → ``RuntimeError``
          （同 key 不同 preimage 冲突）。
        - 不存在 → root 必须存在、active 且属于本 workspace；递归 CTE
          冻结子树全部 node（含 root）的 (node_id, revision) 与每个
          session 的不可变 locator；**子树内任一节点非 active →
          ``RuntimeError``**（子树已有 deleting 节点，拒绝新删除）；插入
          ``state='preparing'``。本方法**不做任何节点状态变更**——整树
          deleting 由 :meth:`mark_subtree_deleting` 的单事务 CAS 完成。
        """
        self._validate_idempotency_key(idempotency_key)
        _validate_workspace_id(workspace_id)
        validate_session_id(root_node_id)
        with self._write_transaction() as connection:
            existing = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if existing is not None:
                record = self._subtree_delete_record_from_row(existing)
                if (
                    record.workspace_id != workspace_id
                    or record.root_node_id != root_node_id
                ):
                    raise RuntimeError(
                        "subtree delete record preimage 冲突（同 key 不同 "
                        "workspace/root，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_workspace={record.workspace_id!r}, "
                        f"existing_root={record.root_node_id!r}, "
                        f"requested_workspace={workspace_id!r}, "
                        f"requested_root={root_node_id!r}"
                    )
                return record
            # 插入路径：事务内先验证后写入。
            root = self._require_node(connection, root_node_id)
            if str(root["workspace_id"]) != workspace_id:
                raise RuntimeError(
                    "root 节点属于其他 workspace，拒绝跨 workspace 删除: "
                    f"root={root_node_id}, root_workspace={root['workspace_id']!r}, "
                    f"requested_workspace={workspace_id!r}"
                )
            if str(root["state"]) != "active":
                raise RuntimeError(
                    "root 节点非 active，拒绝新删除: "
                    f"root={root_node_id}, state={root['state']!r}"
                )
            rows = self._subtree_rows(connection, root_node_id)
            frozen_nodes: list[SubtreeFrozenNode] = []
            locators: dict[str, str] = {}
            for subtree_row in rows:
                if str(subtree_row["workspace_id"]) != workspace_id:
                    raise RuntimeError(
                        "子树内存在跨 workspace 节点（目录不一致，fail closed）: "
                        f"node_id={subtree_row['node_id']}, "
                        f"node_workspace={subtree_row['workspace_id']!r}, "
                        f"requested_workspace={workspace_id!r}"
                    )
                if str(subtree_row["state"]) != "active":
                    raise RuntimeError(
                        "子树内存在非 active 节点，拒绝新删除: "
                        f"node_id={subtree_row['node_id']}, "
                        f"state={subtree_row['state']!r}"
                    )
                frozen_nodes.append(
                    SubtreeFrozenNode(
                        node_id=str(subtree_row["node_id"]),
                        revision=int(subtree_row["revision"]),
                    )
                )
                if str(subtree_row["kind"]) == "session":
                    locator = subtree_row["storage_relative_locator"]
                    if locator is None:
                        # DDL CHECK 已保证 session 行必有 locator；防御性 fail closed。
                        raise RuntimeError(
                            "session 节点缺少 storage_relative_locator"
                            f"（目录被外部改动，fail closed）: "
                            f"node_id={subtree_row['node_id']}"
                        )
                    validate_storage_relative_locator(str(locator))
                    locators[str(subtree_row["node_id"])] = str(locator)
            frozen_nodes.sort(key=lambda item: item.node_id)
            record_created_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO subtree_delete_records ("
                "subtree_delete_idempotency_key, workspace_id, root_node_id, "
                "frozen_node_ids, frozen_session_locators, state, abort_reason, "
                "record_created_at, record_updated_at, drained_session_ids) "
                "VALUES (?, ?, ?, ?, ?, 'preparing', NULL, ?, ?, '[]')",
                (
                    idempotency_key,
                    workspace_id,
                    root_node_id,
                    json.dumps(
                        [
                            {
                                "node_id": item.node_id,
                                "revision": item.revision,
                            }
                            for item in frozen_nodes
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        [
                            {
                                "session_id": session_id,
                                "storage_relative_locator": locator,
                            }
                            for session_id, locator in sorted(locators.items())
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    record_created_at,
                    record_created_at,
                ),
            )
            inserted = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if inserted is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "subtree delete record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(inserted)

    def mark_subtree_deleting(self, idempotency_key: str) -> None:
        """单事务 CAS 把整棵冻结子树 active→deleting（**唯一逻辑可见性关闭点**）。

        record 必须 ``preparing``（已 ``deleting`` → 幂等 no-op；其余状态
        → ``RuntimeError``）。事务内先 CAS 验证：冻结集合内全部节点仍
        存在、active 且 revision 等于冻结值（漂移 → ``RuntimeError`` 含
        期望/实际）；随后同一事务内全部节点置 ``deleting`` 且
        revision+1、record 推进为 ``deleting``。任何失败整体回滚：子树
        保持 active、record 保持 preparing。
        """
        self._validate_idempotency_key(idempotency_key)
        with self._write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "deleting":
                # 幂等重入：整树已 deleting，不重验子树（以首次 mark 提交为准）。
                return
            if state != "preparing":
                raise RuntimeError(
                    "subtree delete record 状态不允许 mark: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            for item in record.frozen_node_ids:
                node = self._fetch_node(connection, item.node_id)
                if node is None:
                    raise RuntimeError(
                        "mark CAS 失败：冻结节点已不存在: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_revision={item.revision}, actual=缺失"
                    )
                if str(node["state"]) != "active":
                    raise RuntimeError(
                        "mark CAS 失败：冻结节点非 active: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_state='active', actual_state={node['state']!r}"
                    )
                actual_revision = int(node["revision"])
                if actual_revision != item.revision:
                    raise RuntimeError(
                        "mark CAS 失败：冻结节点 revision 已漂移: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_revision={item.revision}, "
                        f"actual_revision={actual_revision}"
                    )
            for item in record.frozen_node_ids:
                connection.execute(
                    "UPDATE nodes SET state = 'deleting', revision = revision + 1 "
                    "WHERE node_id = ?",
                    (item.node_id,),
                )
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'deleting', "
                "record_updated_at = ? WHERE subtree_delete_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )

    def record_drain_progress(self, idempotency_key: str, session_id: str) -> None:
        """记录单个 session 的物理隔离进度；首次调用把 record → draining。

        record 必须 ``deleting``/``draining``；session 必须在冻结集合内；
        已记录过 → 幂等 no-op。进度存 ``drained_session_ids``（JSON 数组
        追加），保证 drain 中途崩溃重入按 record 定点继续、不重查当前树。
        """
        self._validate_idempotency_key(idempotency_key)
        validate_session_id(session_id)
        with self._write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state not in ("deleting", "draining"):
                raise RuntimeError(
                    "subtree delete record 状态不允许记录 drain 进度: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            if session_id not in record.frozen_session_locators:
                raise RuntimeError(
                    "drain 进度的 session 不在冻结集合内（拒绝记录）: "
                    f"key={idempotency_key!r}, session_id={session_id!r}"
                )
            if session_id in record.drained_session_ids:
                return
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'draining', "
                "drained_session_ids = ?, record_updated_at = ? "
                "WHERE subtree_delete_idempotency_key = ?",
                (
                    json.dumps(
                        [*record.drained_session_ids, session_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    datetime.now(UTC).isoformat(),
                    idempotency_key,
                ),
            )

    def finish_subtree_delete(self, idempotency_key: str) -> None:
        """单事务终结删除：drain 完整性校验 + 全树 tombstone（行删除）。

        record 必须 ``draining``；无 session 的空子树允许 ``deleting``
        直接 finish（drained 集合已等于冻结 session 集合=∅，无 drain 需
        要）。``drained_session_ids`` 与冻结 session 集合不一致 →
        ``RuntimeError``（未完成）。校验通过后同一事务内按深度降序删除
        nodes 表全部冻结行（tombstone=行删除；深度序满足 ``parent_node_id``
        自引用外键）并把 record 推进为 ``completed``。
        """
        self._validate_idempotency_key(idempotency_key)
        with self._write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state not in ("deleting", "draining"):
                raise RuntimeError(
                    "subtree delete record 状态不允许 finish: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            frozen_sessions = set(record.frozen_session_locators)
            drained = set(record.drained_session_ids)
            if drained != frozen_sessions:
                raise RuntimeError(
                    "subtree delete drain 未完成，拒绝 tombstone: "
                    f"key={idempotency_key!r}, "
                    f"missing={sorted(frozen_sessions - drained)}, "
                    f"unexpected={sorted(drained - frozen_sessions)}"
                )
            frozen_ids = [item.node_id for item in record.frozen_node_ids]
            frozen_set = set(frozen_ids)
            placeholders = ",".join("?" for _ in frozen_ids)
            parent_rows = connection.execute(
                f"SELECT node_id, parent_node_id FROM nodes "
                f"WHERE node_id IN ({placeholders})",
                tuple(frozen_ids),
            ).fetchall()
            parent_of: dict[str, str | None] = {
                str(item["node_id"]): (
                    str(item["parent_node_id"])
                    if item["parent_node_id"] is not None
                    else None
                )
                for item in parent_rows
            }
            depth_by_id: dict[str, int] = {}
            for node_id in frozen_ids:
                chain: list[str] = []
                current: str | None = node_id
                while (
                    current is not None
                    and current in frozen_set
                    and current not in depth_by_id
                ):
                    chain.append(current)
                    current = parent_of.get(current)
                if current is not None and current in depth_by_id:
                    base = depth_by_id[current]
                else:
                    base = -1
                for member in reversed(chain):
                    base += 1
                    depth_by_id[member] = base
            for node_id in sorted(frozen_ids, key=lambda n: (-depth_by_id[n], n)):
                cursor = connection.execute(
                    "DELETE FROM nodes WHERE node_id = ?", (node_id,)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "finish 失败：冻结节点行已缺失（外部改动，fail closed）: "
                        f"key={idempotency_key!r}, node_id={node_id}"
                    )
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'completed', "
                "record_updated_at = ? WHERE subtree_delete_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )

    def abort_subtree_delete(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SubtreeDeleteRecord:
        """终结删除 record：preparing/deleting/draining → aborted（记 reason）。

        ``completed`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。**注意**：``deleting``/
        ``draining`` 状态的 abort 不回滚节点状态——design.md 明确「不回滚
        active」，节点保持 deleting 待人工/新操作处置；本方法只终结
        record，不动 nodes 表。
        """
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self._write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "completed":
                raise RuntimeError(
                    "subtree delete record 已完成，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._subtree_delete_record_from_row(row)
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'aborted', "
                "abort_reason = ?, record_updated_at = ? "
                "WHERE subtree_delete_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "subtree delete record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(updated)

    def get_subtree_delete_record(self, idempotency_key: str) -> SubtreeDeleteRecord:
        """按幂等键返回 subtree delete record 投影；不存在抛 KeyError。"""
        self._validate_idempotency_key(idempotency_key)
        with self._read_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(row)

    def delete_empty_folder(self, folder_id: str) -> None:
        """空 folder 的非递归简单删除（design.md §9 约 776 行允许面）。

        folder 必须存在、active 且无任何直接子节点；非 folder 节点、
        非 active 状态、有子节点（含 deleting 子节点）一律
        ``RuntimeError``——「非空 folder 的非递归删除仍明确拒绝，不能因
        folder 无物理目录就悄悄丢弃子节点」；session 节点的删除走子树
        删除协议，不经本方法。
        """
        validate_session_id(folder_id)
        with self._write_transaction() as connection:
            row = self._require_node(connection, folder_id)
            if str(row["kind"]) != "folder":
                raise RuntimeError(
                    "非 folder 节点拒绝非递归删除（session 走子树删除协议）: "
                    f"node_id={folder_id}, kind={row['kind']!r}"
                )
            if str(row["state"]) != "active":
                raise RuntimeError(
                    f"folder 非 active，拒绝删除: node_id={folder_id}, "
                    f"state={row['state']!r}"
                )
            child_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE parent_node_id = ?",
                    (folder_id,),
                ).fetchone()[0]
            )
            if child_count > 0:
                raise RuntimeError(
                    "非空 folder 的非递归删除被明确拒绝: "
                    f"folder_id={folder_id}, child_count={child_count}"
                )
            connection.execute("DELETE FROM nodes WHERE node_id = ?", (folder_id,))

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def count_children(self, node_id: str) -> int:
        """返回直接子节点数。"""
        with self._read_transaction() as connection:
            self._require_node(connection, node_id)
            row = connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE parent_node_id = ?",
                (node_id,),
            ).fetchone()
            return int(row[0])

    def get_node(self, node_id: str) -> SessionCatalogNode:
        """返回节点投影；不存在抛 KeyError。"""
        with self._read_transaction() as connection:
            return self._node_from_row(self._require_node(connection, node_id))

    def list_children(
        self,
        parent_node_id: str | None,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[SessionCatalogNode], str | None, bool]:
        """按 node_id 游标稳定分页返回直接子节点。

        ``parent_node_id`` 为 None 时返回根级节点；``cursor`` 是上一页最后
        一个 node_id，下一页从其之后开始。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"limit 必须是正整数: {limit!r}")
        with self._read_transaction() as connection:
            if parent_node_id is not None:
                self._require_node(connection, parent_node_id)
            if cursor is None:
                rows = connection.execute(
                    f"SELECT {_NODE_COLUMNS} FROM nodes "
                    "WHERE parent_node_id IS ? ORDER BY node_id LIMIT ?",
                    (parent_node_id, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_NODE_COLUMNS} FROM nodes "
                    "WHERE parent_node_id IS ? AND node_id > ? "
                    "ORDER BY node_id LIMIT ?",
                    (parent_node_id, cursor, limit + 1),
                ).fetchall()
            has_more = len(rows) > limit
            items = [self._node_from_row(row) for row in rows[:limit]]
            next_cursor = items[-1].node_id if has_more and items else None
            return items, next_cursor, has_more

    def breadcrumb(self, node_id: str) -> list[SessionCatalogNode]:
        """返回从根到该节点（含自身）的节点链。"""
        with self._read_transaction() as connection:
            chain: list[SessionCatalogNode] = []
            visited: set[str] = set()
            current_id: str | None = node_id
            while current_id is not None:
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                row = self._require_node(connection, current_id)
                chain.append(self._node_from_row(row))
                current_id = row["parent_node_id"]
            chain.reverse()
            return chain

    def nearest_session_ancestor(self, node_id: str) -> str | None:
        """返回最近的 session 祖先 ID（不含自身；传 parent 语义）。

        从父节点开始向上找第一个 kind 为 session 的祖先。
        """
        with self._read_transaction() as connection:
            row = self._require_node(connection, node_id)
            current_id = row["parent_node_id"]
            visited: set[str] = set()
            while current_id is not None:
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                current_row = self._require_node(connection, current_id)
                if current_row["kind"] == "session":
                    return str(current_row["node_id"])
                current_id = current_row["parent_node_id"]
            return None

    def descendant_session_ids(self, node_id: str) -> list[str]:
        """递归 CTE 返回全部后代 session ID（不含自身，按 node_id 排序）。"""
        with self._read_transaction() as connection:
            self._require_node(connection, node_id)
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(node_id, kind) AS (
                    SELECT node_id, kind FROM nodes WHERE parent_node_id = ?
                    UNION
                    SELECT n.node_id, n.kind FROM nodes n
                    JOIN descendants d ON n.parent_node_id = d.node_id
                )
                SELECT node_id FROM descendants WHERE kind = 'session'
                ORDER BY node_id
                """,
                (node_id,),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def get_session_by_main_thread(
        self,
        workspace_id: str,
        main_thread_id: str,
    ) -> SessionCatalogNode:
        """按 (workspace_id, main_thread_id) 返回唯一 session 节点。"""
        with self._read_transaction() as connection:
            row = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes "
                "WHERE workspace_id = ? AND main_thread_id = ? AND kind = 'session'",
                (workspace_id, main_thread_id),
            ).fetchone()
            if row is None:
                raise KeyError(
                    "main_thread_id 对应的 session 不存在: "
                    f"workspace_id={workspace_id}, main_thread_id={main_thread_id}"
                )
            return self._node_from_row(row)

    def verify_workspace_consistency(self) -> None:
        """全表校验：父节点存在、父子同 workspace、无环；违反抛 RuntimeError。"""
        with self._read_transaction() as connection:
            rows = connection.execute(
                "SELECT node_id, parent_node_id, workspace_id FROM nodes"
            ).fetchall()
        by_id = {str(row["node_id"]): row for row in rows}
        for row in rows:
            parent_node_id = row["parent_node_id"]
            if parent_node_id is None:
                continue
            parent = by_id.get(str(parent_node_id))
            if parent is None:
                raise RuntimeError(
                    "会话目录父节点缺失: "
                    f"node_id={row['node_id']}, parent_node_id={parent_node_id}"
                )
            if str(parent["workspace_id"]) != str(row["workspace_id"]):
                raise RuntimeError(
                    "会话目录父子节点 workspace 不一致: "
                    f"node_id={row['node_id']}, "
                    f"node_workspace={row['workspace_id']}, "
                    f"parent_node_id={parent_node_id}, "
                    f"parent_workspace={parent['workspace_id']}"
                )
        for row in rows:
            visited: set[str] = set()
            current = row
            while current["parent_node_id"] is not None:
                current_id = str(current["node_id"])
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                current = by_id[str(current["parent_node_id"])]

    def resolve_session_locator(self, locator: str) -> Path:
        """解析 locator 为 sessions_root 下的绝对路径；先过形态与预算校验。"""
        validate_storage_relative_locator(locator)
        relative = locator[len(_LOCATOR_PREFIX):]
        validate_path_budget(self.sessions_root, relative)
        return self.sessions_root / relative
