"""per-session ``session-control.sqlite`` 的最小基础设施（OpenSpec 8.2 切片2）。

本模块只提供 8.2 迁移机器初始化 per-session 控制库所需的最小表结构与
幂等初始化/读取/校验 API；**不切权威、不装配**——生产运行时（resolver、
container/main.py）不感知本模块，权威切换属切片3 装配。

**8.5-A 扩展（R20 已落地）**：本库承载 child thread 创建机器的持久状态
——``thread_creation_records``（承担 operation lease 的 create-or-get
record，**不进入 thread catalog**）与 ``thread_execution_intents``（初始
execution 的持久 admission intent，本轮只落库不绑定真实 Job，``bound``
状态保留给 8.5 worker）；``thread_catalog`` 的 ``kind`` CHECK 由
``('main')`` 扩为 ``('main', 'child')``（SCHEMA_VERSION 1→2，v1 库在
``_initialize`` 单事务内以「建临时新表→拷贝→校验→删旧→改名」原地升级，
既有 main row 数据零丢失）。``lifecycle_fence`` 表语义不变。
``thread_catalog`` 的 main row 仍唯一——读取一律按 ``kind='main'`` 过滤，
child row 只由 :meth:`publish_thread_creation_record` 在单事务内插入
（唯一可见性提交点）。

错误分类约定（沿用 ``session_catalog_store.py``）：

- ``TypeError``：输入类型错误（非字符串 ID、非 datetime 的 created_at）。
- ``ValueError``：输入形态非法（ID 不满足 canonical profile、fence 状态
  越界、generation 非法、idempotency_key 含路径分隔符、locator 形态
  非法）。
- ``KeyError``：目标行不存在（main row / fence row / record / intent
  缺失）。
- ``RuntimeError``：语义冲突（main row 已存在但 thread_id 不一致、fence
  已存在但 state/generation 不一致、多 main row、指针校验不符、同 key
  不同 preimage、publish CAS 失败、record 终态冲突、库被外部改动）。

连接约定（沿用 ``session_catalog_store.py``）：WAL、``foreign_keys``、
``busy_timeout``、``sqlite3.Row``、``PRAGMA user_version`` 非
0/1/``SCHEMA_VERSION`` 时 fail-closed 拒绝打开。
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import (
    validate_session_id,
    validate_thread_id,
)
from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS

__all__ = [
    "SessionControlStore",
    "ThreadCreationRecord",
    "ThreadExecutionIntent",
    "validate_thread_relative_locator",
]

_FENCE_ROW_ID = 1

# thread_catalog（8.5-A 升级为 v2 形态）：kind CHECK 由 ``('main')`` 扩为
# ``('main', 'child')``。SQLite 不支持原地修改 CHECK 约束，v1 库由
# ``_initialize`` 在单事务内以「建临时新表→拷贝→校验→删旧→改名」原地
# 升级（见 :meth:`_upgrade_thread_catalog_kind_v1_to_v2`）。
_THREAD_CATALOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_catalog (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'child')),
    created_at TEXT NOT NULL
)
"""

# v1→v2 升级临时表（普通 CREATE，不带 IF NOT EXISTS；升级完成后 RENAME
# 回 ``thread_catalog``，任何失败随 ``_initialize`` 事务整体回滚）。
_THREAD_CATALOG_KIND_UPGRADE_TABLE_DDL = """
CREATE TABLE thread_catalog_kind_upgrade (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'child')),
    created_at TEXT NOT NULL
)
"""

_LIFECYCLE_FENCE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_fence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    generation INTEGER NOT NULL
)
"""

# ThreadCreationRecord（8.5-A，R20）：child thread 创建流 operation lease
# record。与 workspace catalog 的 session_creation_records 同构：record
# 在建立任何 staging 前 create-or-get（gate 内短事务）、不进入
# thread_catalog、冻结创建身份/precondition；publish 是唯一可见性提交点
# （thread_catalog child row 与 record→published 在同一事务）；CAS 失败
# 只定点清理 record 列出的目录并 abort，不发布、不重基。
# 列序说明：任务书 §2.1-A 给出的 16 列逐字保留（名称/顺序/约束），其间的
# ``artifact_manifest`` / ``graph_binding`` / ``capability_profile`` /
# ``task_seed`` / ``task_reference`` / ``child_created_at`` 为加法列
# （tasks.md 8.5-A 冻结项 GraphBinding/capability/seed/reference、artifact
# manifest 本体与 child UTC created_at——恢复只按预存 record 校验，需要
# 清单本体而非仅 hash），字段映射见 rounds/R20-impl.md §6。
_THREAD_CREATION_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_creation_records (
    thread_creation_idempotency_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published', 'aborted')),
    preimage_hash TEXT NOT NULL,
    delegation_id TEXT,
    child_thread_id TEXT NOT NULL UNIQUE,
    final_relative_locator TEXT NOT NULL UNIQUE,
    staging_locator TEXT NOT NULL,
    artifact_manifest TEXT,
    artifact_manifest_hash TEXT,
    graph_binding TEXT NOT NULL,
    capability_profile TEXT NOT NULL,
    task_seed TEXT,
    task_reference TEXT,
    owner_session_lifecycle_generation INTEGER NOT NULL,
    catalog_precondition_revision INTEGER NOT NULL,
    collaboration_precondition_revision INTEGER,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    admission_intent TEXT NOT NULL,
    abort_reason TEXT,
    child_created_at TEXT NOT NULL,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
"""

# delegated child 将 delegation_id 纳入唯一约束（tasks.md 8.5-A）：同一
# delegation 至多绑定一条创建 record（含 aborted——失败后须换新
# delegation 重试，不静默改绑）。
_IDX_THREAD_CREATION_DELEGATION_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_creation_delegation "
    "ON thread_creation_records(delegation_id) WHERE delegation_id IS NOT NULL"
)

_THREAD_CREATION_RECORD_COLUMNS = (
    "thread_creation_idempotency_key, state, preimage_hash, delegation_id, "
    "child_thread_id, final_relative_locator, staging_locator, "
    "artifact_manifest, artifact_manifest_hash, graph_binding, "
    "capability_profile, task_seed, task_reference, "
    "owner_session_lifecycle_generation, catalog_precondition_revision, "
    "collaboration_precondition_revision, initial_state, admission_intent, "
    "abort_reason, child_created_at, record_created_at, record_updated_at"
)

# 初始 execution 的持久 admission intent（8.5-A 接口契约，R20）：发布后由
# 幂等 worker create-or-get；本轮只落库不绑定真实 Job（state 恒
# 'pending'，'bound' 保留给 8.5 Job 绑定实现）。
_THREAD_EXECUTION_INTENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_execution_intents (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'bound')),
    intent_created_at TEXT NOT NULL,
    intent_updated_at TEXT NOT NULL
)
"""

# 每个 child thread 至多一条初始 execution intent（崩溃不能留下重复初始
# Job 的持久侧保证）。
_IDX_THREAD_EXECUTION_INTENT_THREAD_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_execution_intent_thread "
    "ON thread_execution_intents(thread_id)"
)

_THREAD_EXECUTION_INTENT_COLUMNS = (
    "admission_idempotency_key, session_id, thread_id, "
    "creation_idempotency_key, initial_state, state, "
    "intent_created_at, intent_updated_at"
)

_FENCE_STATES = ("active", "deleting")

_THREAD_CREATION_RECORD_STATES = ("preparing", "published", "aborted")

_INITIAL_STATE_VALUES = ("running", "idle")

# thread 最终 relative locator（相对 owner session 目录）：
# ``threads/YYYY/MM/DD/{thread_id}``，日期为 child UTC 创建日。
_THREAD_RELATIVE_LOCATOR_PATTERN = re.compile(
    r"^threads/(\d{4})/(\d{2})/(\d{2})/(thr_[0-9a-f]{32})$"
)

# sha256 小写 hex（artifact manifest hash 形态约束）。
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_thread_relative_locator(value: str) -> None:
    """校验 thread 最终 relative locator（相对 owner session 目录）。

    形态 ``threads/YYYY/MM/DD/{thread_id}``：与
    ``validate_storage_relative_locator`` 同口径——YYYY 4 位数字、MM
    01-12、DD 按 ``calendar.monthrange`` 对应月份合法（含闰年），叶名
    thread_id 过完整验证器；child thread 按自身不可变 UTC ``created_at``
    分桶（design.md §9），不额外加 hash shard。
    """
    if not isinstance(value, str):
        raise TypeError(f"thread relative locator 必须是字符串: {value!r}")
    match = _THREAD_RELATIVE_LOCATOR_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"thread relative locator 形态非法: {value!r}")
    year_text, month_text, day_text, thread_id = match.groups()
    validate_thread_id(thread_id)
    month = int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"thread relative locator 月份非法: {value!r}")
    day = int(day_text)
    _, last_day = calendar.monthrange(int(year_text), month)
    if not 1 <= day <= last_day:
        raise ValueError(f"thread relative locator 日期非法: {value!r}")


@dataclass(frozen=True, slots=True)
class ThreadCreationRecord:
    """thread_creation_records 表行的不可变投影（8.5-A 创建 lease record）。

    ``state`` 闭集为 ``preparing/published/aborted``；record 冻结 child
    身份（``child_thread_id``/``child_created_at``）、最终与内部 staging
    locator、GraphBinding/capability/seed/reference（canonical JSON 文
    本）、artifact manifest 清单本体与 hash、owner Session lifecycle
    generation 与 catalog/collaboration precondition revision；publish
    时在同一事务 CAS 校验后插入 thread_catalog child row（唯一可见性
    提交点）。``artifact_manifest`` 是「预期内容清单」（相对路径 →
    sha256）的 canonical JSON 文本，``artifact_manifest_hash`` 是该文本
    的 sha256——清单本体随 record 冻结，恢复只按预存 record 校验。
    """

    thread_creation_idempotency_key: str
    state: str
    preimage_hash: str
    delegation_id: str | None
    child_thread_id: str
    final_relative_locator: str
    staging_locator: str
    artifact_manifest: str | None
    artifact_manifest_hash: str | None
    graph_binding: str
    capability_profile: str
    task_seed: str | None
    task_reference: str | None
    owner_session_lifecycle_generation: int
    catalog_precondition_revision: int
    collaboration_precondition_revision: int | None
    initial_state: str
    admission_intent: str
    abort_reason: str | None
    child_created_at: str
    record_created_at: str
    record_updated_at: str


@dataclass(frozen=True, slots=True)
class ThreadExecutionIntent:
    """thread_execution_intents 表行的不可变投影（8.5-A admission intent）。

    ``state`` 闭集为 ``pending/bound``；本轮（R20）只写 ``pending``，
    ``bound`` 由 8.5 的 Job 绑定实现推进。``admission_idempotency_key``
    是 worker 幂等键（创建流以 creation idempotency key 派生），
    ``thread_id`` 上有唯一索引——同一 child thread 至多一条初始 execution
    intent，崩溃不会留下重复初始 Job 的持久侧入口。
    """

    admission_idempotency_key: str
    session_id: str
    thread_id: str
    creation_idempotency_key: str
    initial_state: str
    state: str
    intent_created_at: str
    intent_updated_at: str


class SessionControlStore:
    """单 session 控制库：thread catalog（main + child）+ lifecycle fence
    + thread creation record / execution intent journal。

    构造时创建 database_path 父目录并幂等建表；``user_version`` 只允许
    0（新库，初始化后写 2）、1（R12/R13 v1 库，单事务加法升级后写 2）
    或 2，其余版本 fail-closed 拒绝打开。
    """

    SCHEMA_VERSION = 2

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError(
                f"database_path 必须是 Path: {database_path!r}"
            )
        self.database_path = database_path.expanduser().resolve()
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

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE 内先验证后写入。

        正常退出（含事务体内 ``return``）一律 COMMIT，任何异常 ROLLBACK
        后原样抛出——模式对齐 ``session_catalog_store._write_transaction``，
        吸取 R14 审查 M1 教训：禁止在打开事务内裸 ``return`` 造成事务
        泄漏（本类既有方法以「先判态后写入 + 成功路径末尾 COMMIT」的
        手写模式保持不变；8.5-A 新增方法统一走本 CM）。
        """
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
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

    def _initialize(self) -> None:
        """幂等建表并设置 user_version；未知版本 fail-closed 拒绝打开。

        支持的 current：0（全新库，建全部表并置 v2）、1（R12/R13 v1 库，
        同一事务内先以「建临时新表→拷贝→校验→删旧→改名」升级
        ``thread_catalog`` 的 kind CHECK、再幂等补建 8.5-A 新表并升 v2，
        既有 main row 数据零丢失）、2（已是当前版本；全部 DDL 均为
        ``CREATE ... IF NOT EXISTS`` 幂等 no-op）。
        """
        current = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current not in (0, 1, self.SCHEMA_VERSION):
            raise RuntimeError(
                "session control schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if current == 1:
                self._upgrade_thread_catalog_kind_v1_to_v2()
            self._connection.execute(_THREAD_CATALOG_TABLE_DDL)
            self._connection.execute(_LIFECYCLE_FENCE_TABLE_DDL)
            self._connection.execute(_THREAD_CREATION_RECORDS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_CREATION_DELEGATION_DDL)
            self._connection.execute(_THREAD_EXECUTION_INTENTS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_EXECUTION_INTENT_THREAD_DDL)
            if current != self.SCHEMA_VERSION:
                self._connection.execute(
                    f"PRAGMA user_version = {self.SCHEMA_VERSION}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _upgrade_thread_catalog_kind_v1_to_v2(self) -> None:
        """v1→v2 加法升级：``thread_catalog`` kind CHECK 原地重建。

        SQLite 不支持修改既有列的 CHECK 约束，按任务书 §2.1-A 定死的
        方式一在单事务内执行「``CREATE`` 临时新表 → ``INSERT…SELECT``
        拷贝全部行 → 行数与 kind 合法性校验 → ``DROP`` 旧表 →
        ``RENAME`` 回原名」；拷贝前后行数必须相等（既有 main row 数据
        零丢失），任何失败随 ``_initialize`` 的事务整体回滚（
        ``user_version`` 保持 1，库可原样重开，fail closed）。
        """
        self._connection.execute(_THREAD_CATALOG_KIND_UPGRADE_TABLE_DDL)
        before = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog"
            ).fetchone()[0]
        )
        self._connection.execute(
            "INSERT INTO thread_catalog_kind_upgrade "
            "(thread_id, kind, created_at) "
            "SELECT thread_id, kind, created_at FROM thread_catalog"
        )
        after = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog_kind_upgrade"
            ).fetchone()[0]
        )
        if before != after:
            raise RuntimeError(
                "thread_catalog v1→v2 升级拷贝行数不一致（数据零丢失保证"
                f"被破坏，事务将回滚）: path={self.database_path}, "
                f"before={before}, after={after}"
            )
        invalid = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog_kind_upgrade "
                "WHERE kind NOT IN ('main', 'child')"
            ).fetchone()[0]
        )
        if invalid != 0:
            raise RuntimeError(
                "thread_catalog v1→v2 升级发现非法 kind 行（库被外部改动，"
                f"fail closed）: path={self.database_path}, invalid={invalid}"
            )
        self._connection.execute("DROP TABLE thread_catalog")
        self._connection.execute(
            "ALTER TABLE thread_catalog_kind_upgrade RENAME TO thread_catalog"
        )

    # ------------------------------------------------------------------
    # 写操作（事务内先验证后写入，异常回滚）
    # ------------------------------------------------------------------

    def initialize_main_thread(self, thread_id: str, created_at: datetime) -> None:
        """create-or-get 唯一 main row：已存在且一致 → 幂等；不一致 → fail closed。

        ``thread_catalog`` 只允许 ``kind='main'``（CHECK 冻结）；main row
        的 thread_id 必须与 workspace catalog 冻结的 ``main_thread_id``
        一致（由 :meth:`verify_matches_catalog_main_thread` 复验），本方法
        保证同库内不会出现第二个 thread_id 的 main row。
        """
        validate_thread_id(thread_id)
        if not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        created_at_text = created_at.isoformat()
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # R20：thread_catalog 现同时承载 child row（8.5-A），main row
            # 判定一律按 kind='main' 过滤——child row 的存在不影响 main
            # row 的 create-or-get 语义（唯一 main row 语义不变）。
            rows = self._connection.execute(
                "SELECT thread_id, kind, created_at FROM thread_catalog "
                "WHERE kind = 'main'"
            ).fetchall()
            if not rows:
                self._connection.execute(
                    "INSERT INTO thread_catalog (thread_id, kind, created_at) "
                    "VALUES (?, 'main', ?)",
                    (thread_id, created_at_text),
                )
            else:
                if len(rows) > 1:
                    raise RuntimeError(
                        "session control 出现多个 main row（库被外部改动，"
                        f"fail closed）: path={self.database_path}, "
                        f"rows={[str(row['thread_id']) for row in rows]}"
                    )
                existing = rows[0]
                if (
                    str(existing["thread_id"]) != thread_id
                    or str(existing["kind"]) != "main"
                ):
                    raise RuntimeError(
                        "main row 已存在且与初始化参数不一致（拒绝覆盖，"
                        f"fail closed）: path={self.database_path}, "
                        f"existing_thread_id={existing['thread_id']!r}, "
                        f"existing_kind={existing['kind']!r}, "
                        f"requested_thread_id={thread_id!r}"
                    )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def initialize_fence(self, state: str = "active", generation: int = 1) -> None:
        """create-or-get 单行 fence（id=1）：已存在且一致 → no-op；不一致 → fail closed。"""
        if state not in _FENCE_STATES:
            raise ValueError(f"fence 状态非法: {state!r}")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise TypeError(f"fence generation 必须是整数: {generation!r}")
        if generation < 0:
            raise ValueError(f"fence generation 不能为负: {generation!r}")
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
                (_FENCE_ROW_ID,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO lifecycle_fence (id, state, generation) "
                    "VALUES (?, ?, ?)",
                    (_FENCE_ROW_ID, state, generation),
                )
            elif (
                str(row["state"]) != state
                or int(row["generation"]) != generation
            ):
                raise RuntimeError(
                    "fence 已存在且与初始化参数不一致（拒绝覆盖，fail closed）: "
                    f"path={self.database_path}, "
                    f"existing_state={row['state']!r}, "
                    f"existing_generation={row['generation']!r}, "
                    f"requested_state={state!r}, "
                    f"requested_generation={generation!r}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def cas_fence_transition(self, expected_generation: int, new_state: str) -> bool:
        """CAS 推进 fence 状态（语义对齐 R10
        ``SessionLifecycleFence.cas_transition``，8.1-B 删除流 drain 使用）。

        - ``active → deleting`` 且 ``generation`` 匹配 → 推进为
          ``deleting`` 并 generation+1，返回 ``True``；
        - ``deleting → active`` 恒拒绝（不可复活）返回 ``False``；
        - generation 不匹配返回 ``False``（fence 保持不变）；
        - 非法 ``new_state`` 抛 ``ValueError``；
        - fence row 缺失抛 ``KeyError``（fail closed，与 :meth:`get_fence`
          一致——缺失的 fence 不是可 CAS 的 active fence）。

        事务纪律（R14 审查 M1 修复）：先以只读查询判态，False / KeyError
        失败路径在开启任何写事务前返回或抛出，不遗留打开事务、不持有
        RESERVED 写锁；仅确定要推进时才 ``BEGIN IMMEDIATE``，并在写事务
        内复核判态后 UPDATE——复核不匹配时 ROLLBACK 后返回 ``False``，
        推进判定与写入始终同处一个写事务（CAS 原子性不受先读后写影响）。
        """
        if new_state not in _FENCE_STATES:
            raise ValueError(f"fence 目标状态非法: {new_state!r}")
        if not isinstance(expected_generation, int) or isinstance(
            expected_generation, bool
        ):
            raise TypeError(
                f"expected_generation 必须是整数: {expected_generation!r}"
            )
        self._ensure_open()
        # 只读判态：连接为 isolation_level=None，裸 SELECT 不开启事务，
        # 失败路径因此天然无事务可泄漏（同实例可直接重试 CAS）。
        row = self._connection.execute(
            "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
            (_FENCE_ROW_ID,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"session control 缺少 lifecycle fence row: "
                f"path={self.database_path}"
            )
        state = str(row["state"])
        generation = int(row["generation"])
        # active→deleting 是唯一合法转移（deleting→active 不可复活）。
        if state != "active" or new_state != "deleting":
            return False
        if generation != expected_generation:
            return False
        # 确定要推进才开写事务；事务内复核，防止只读判态与 BEGIN
        # IMMEDIATE 之间 fence 被其他写者改变导致误推进。
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
                (_FENCE_ROW_ID,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"session control 缺少 lifecycle fence row: "
                    f"path={self.database_path}"
                )
            state = str(row["state"])
            generation = int(row["generation"])
            proceed = (
                state == "active"
                and new_state == "deleting"
                and generation == expected_generation
            )
            if proceed:
                self._connection.execute(
                    "UPDATE lifecycle_fence SET state = ?, generation = ? "
                    "WHERE id = ?",
                    (new_state, generation + 1, _FENCE_ROW_ID),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        if not proceed:
            # 复核不匹配（并发穿插）：整体回滚，fence 保持不变。
            self._connection.execute("ROLLBACK")
            return False
        self._connection.execute("COMMIT")
        return True

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def get_main_thread(self) -> sqlite3.Row:
        """返回唯一 main row；缺失抛 KeyError，多行抛 RuntimeError（库损坏）。

        R20：按 ``kind='main'`` 过滤——thread_catalog 现同时承载 child
        row（8.5-A），child row 的存在不改变 main row 读取语义。
        """
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT thread_id, kind, created_at FROM thread_catalog "
            "WHERE kind = 'main'"
        ).fetchall()
        if not rows:
            raise KeyError(
                f"session control 缺少 main row: path={self.database_path}"
            )
        if len(rows) > 1:
            raise RuntimeError(
                "session control 出现多个 main row（库被外部改动，fail closed）: "
                f"path={self.database_path}, "
                f"rows={[str(row['thread_id']) for row in rows]}"
            )
        return rows[0]

    def get_published_child_thread_locator(self, thread_id: str) -> str:
        """返回已发布 child thread 的冻结 locator。

        ``thread_catalog`` 只提供 child 的可见性提交点，物理定位必须继续
        读取同一发布事务冻结的 ``thread_creation_records``。缺少任一行、
        record 未发布或两者的创建时间不一致，都表示控制库被外部改动，
        解析器必须 fail closed。
        """
        validate_thread_id(thread_id)
        self._ensure_open()
        row = self._connection.execute(
            "SELECT tc.kind, tc.created_at, "
            "tcr.state, tcr.final_relative_locator, tcr.child_created_at "
            "FROM thread_catalog AS tc "
            "LEFT JOIN thread_creation_records AS tcr "
            "ON tcr.child_thread_id = tc.thread_id "
            "WHERE tc.thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                "thread catalog 不存在目标 child thread: "
                f"thread_id={thread_id!r}, path={self.database_path}"
            )
        if str(row["kind"]) != "child":
            raise RuntimeError(
                "目标 thread catalog 行不是 child（fail closed）: "
                f"thread_id={thread_id!r}, kind={row['kind']!r}"
            )
        if row["state"] is None:
            raise RuntimeError(
                "child thread 缺少 thread creation record（库被外部改动，"
                f"fail closed）: thread_id={thread_id!r}, "
                f"path={self.database_path}"
            )
        if str(row["state"]) != "published":
            raise RuntimeError(
                "child thread creation record 尚未 published（fail closed）: "
                f"thread_id={thread_id!r}, state={row['state']!r}"
            )
        locator = str(row["final_relative_locator"])
        validate_thread_relative_locator(locator)
        if (
            str(row["created_at"]) != str(row["child_created_at"])
            or not locator.endswith(f"/{thread_id}")
        ):
            raise RuntimeError(
                "child thread catalog 与 creation record 不一致（fail closed）: "
                f"thread_id={thread_id!r}, locator={locator!r}"
            )
        return locator

    def get_fence(self) -> tuple[str, int]:
        """返回 (state, generation)；缺失抛 KeyError。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
            (_FENCE_ROW_ID,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"session control 缺少 lifecycle fence row: path={self.database_path}"
            )
        return str(row["state"]), int(row["generation"])

    def verify_matches_catalog_main_thread(self, main_thread_id: str) -> None:
        """校验 main row thread_id == workspace catalog 冻结的 main_thread_id。

        design.md §9：workspace catalog 的 ``main_thread_id`` 是唯一对外
        权威指针，session-control 只保存与其匹配且 kind 唯一的 main row，
        不维护第二个可独立修改的 main pointer；不符即 fail closed。
        """
        validate_thread_id(main_thread_id)
        row = self.get_main_thread()
        actual = str(row["thread_id"])
        if actual != main_thread_id:
            raise RuntimeError(
                "session control main row 与 workspace catalog 冻结的 "
                f"main_thread_id 不一致（fail closed）: path={self.database_path}, "
                f"control_thread_id={actual!r}, catalog_main_thread_id={main_thread_id!r}"
            )

    # ------------------------------------------------------------------
    # ThreadCreationRecord（8.5-A，R20：child thread 创建流 operation lease）
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_thread_creation_key(idempotency_key: str) -> None:
        """thread creation 幂等键校验：非空且是安全单段路径名。

        幂等键是 session 目录内 ``.staging/<key>/`` staging 目录名（冻结
        进 record 的 ``staging_locator``），含分隔符/``.``/``..`` 会破坏
        定点定位，一律拒绝。
        """
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError(f"idempotency_key 不能为空: {idempotency_key!r}")
        if (
            idempotency_key in (".", "..")
            or "/" in idempotency_key
            or "\\" in idempotency_key
            or "\x00" in idempotency_key
        ):
            raise ValueError(
                "idempotency_key 必须是安全单段路径名（不含分隔符/./..）: "
                f"{idempotency_key!r}"
            )

    @staticmethod
    def _validate_frozen_json_text(value: str, label: str) -> None:
        """冻结列（GraphBinding/capability/seed/reference）形态校验。

        必须是可解析为 JSON 对象（dict）的非空文本——service 序列化，
        store 只做形态闸门（绕过软件直改库时 fail closed）。
        """
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} 必须是非空 JSON 文本: {value!r}")
        try:
            parsed = json.loads(value)
        except ValueError as error:
            raise ValueError(
                f"{label} 不是合法 JSON 文本: {value!r}: {error}"
            ) from error
        if not isinstance(parsed, dict):
            # JSON 文本「内容形态」校验（值本身已是 str，非调用方类型错），
            # 按模块错误分类保持 ValueError（R12 冻结列解析同款 noqa 先例）。
            raise ValueError(  # noqa: TRY004
                f"{label} 必须序列化为 JSON 对象: {value!r}"
            )

    @staticmethod
    def _validate_optional_frozen_json_text(
        value: str | None,
        label: str,
    ) -> None:
        if value is None:
            return
        SessionControlStore._validate_frozen_json_text(value, label)

    def _fetch_thread_creation_record(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_THREAD_CREATION_RECORD_COLUMNS} "
            "FROM thread_creation_records "
            "WHERE thread_creation_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _thread_creation_record_from_row(
        row: sqlite3.Row,
    ) -> ThreadCreationRecord:
        return ThreadCreationRecord(
            thread_creation_idempotency_key=str(
                row["thread_creation_idempotency_key"]
            ),
            state=str(row["state"]),
            preimage_hash=str(row["preimage_hash"]),
            delegation_id=(
                str(row["delegation_id"])
                if row["delegation_id"] is not None
                else None
            ),
            child_thread_id=str(row["child_thread_id"]),
            final_relative_locator=str(row["final_relative_locator"]),
            staging_locator=str(row["staging_locator"]),
            artifact_manifest=(
                str(row["artifact_manifest"])
                if row["artifact_manifest"] is not None
                else None
            ),
            artifact_manifest_hash=(
                str(row["artifact_manifest_hash"])
                if row["artifact_manifest_hash"] is not None
                else None
            ),
            graph_binding=str(row["graph_binding"]),
            capability_profile=str(row["capability_profile"]),
            task_seed=str(row["task_seed"]) if row["task_seed"] is not None else None,
            task_reference=(
                str(row["task_reference"])
                if row["task_reference"] is not None
                else None
            ),
            owner_session_lifecycle_generation=int(
                row["owner_session_lifecycle_generation"]
            ),
            catalog_precondition_revision=int(
                row["catalog_precondition_revision"]
            ),
            collaboration_precondition_revision=(
                int(row["collaboration_precondition_revision"])
                if row["collaboration_precondition_revision"] is not None
                else None
            ),
            initial_state=str(row["initial_state"]),
            admission_intent=str(row["admission_intent"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            child_created_at=str(row["child_created_at"]),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    def create_or_get_thread_creation_record(
        self,
        *,
        idempotency_key: str,
        initial_state: str,
        preimage_hash: str,
        graph_binding: str,
        capability_profile: str,
        created_at: datetime,
        thread_id: str | None = None,
        delegation_id: str | None = None,
        task_seed: str | None = None,
        task_reference: str | None = None,
        collaboration_precondition_revision: int | None = None,
    ) -> ThreadCreationRecord:
        """create-or-get ThreadCreationRecord（gate 内短事务，8.5-A）。

        - 同 key 已存在：``preimage_hash`` 一致 → 返回既有 record（幂等，
          child ID/locator/created_at 等冻结值以既有 record 为准）；
          不一致 → ``RuntimeError``（同 key 不同 preimage 冲突）。传入
          ``thread_id`` 与既有 record 的 child_thread_id 不一致同样拒绝
          （不静默改绑）。
        - 不存在 → 软件分配（或采用传入的已验证 canonical）child
          thread ID（``thr_``）、按 created_at 的 UTC 日期冻结
          ``threads/YYYY/MM/DD/{thread_id}`` 最终 locator 与
          ``.staging/{key}`` 内部 staging locator，读 owner fence
          （必须 active）冻结 generation、按 thread_catalog 行数冻结
          catalog precondition revision，插入 ``state='preparing'``。
          **不进入 thread_catalog、不建立任何 staging 目录**。
        - delegated child 必须携带非空 ``delegation_id``，并受
          ``idx_thread_creation_delegation`` 部分唯一约束（同一
          delegation 至多一条 record，含 aborted）。
        - ``collaboration_precondition_revision`` 本轮恒为 None（
          collaboration ledger 归 8.5）；publish 时遇非空值 fail closed。
        """
        self._validate_thread_creation_key(idempotency_key)
        if initial_state not in _INITIAL_STATE_VALUES:
            raise ValueError(f"initial_state 非法: {initial_state!r}")
        if not isinstance(preimage_hash, str) or not preimage_hash:
            raise ValueError(f"preimage_hash 不能为空: {preimage_hash!r}")
        self._validate_frozen_json_text(graph_binding, "graph_binding")
        self._validate_frozen_json_text(capability_profile, "capability_profile")
        self._validate_optional_frozen_json_text(task_seed, "task_seed")
        self._validate_optional_frozen_json_text(task_reference, "task_reference")
        if not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        if thread_id is not None:
            validate_thread_id(thread_id)
        if delegation_id is not None and (
            not isinstance(delegation_id, str) or not delegation_id
        ):
            raise ValueError(
                "delegated child 必须携带非空 delegation_id: "
                f"{delegation_id!r}"
            )
        if collaboration_precondition_revision is not None and (
            not isinstance(collaboration_precondition_revision, int)
            or isinstance(collaboration_precondition_revision, bool)
        ):
            raise TypeError(
                "collaboration_precondition_revision 必须是整数或 None: "
                f"{collaboration_precondition_revision!r}"
            )
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._fetch_thread_creation_record(
                self._connection, idempotency_key
            )
            if existing is not None:
                if str(existing["preimage_hash"]) != preimage_hash:
                    raise RuntimeError(
                        "thread creation record preimage 冲突（同 key 不同 "
                        "preimage，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={existing['preimage_hash']!r}, "
                        f"requested_preimage={preimage_hash!r}"
                    )
                if (
                    thread_id is not None
                    and str(existing["child_thread_id"]) != thread_id
                ):
                    raise RuntimeError(
                        "thread creation record child ID 冲突（同 key 幂等"
                        "复用时传入 thread_id 与既有 record 不一致，拒绝改"
                        f"绑）: key={idempotency_key!r}, "
                        f"existing_child_thread_id={existing['child_thread_id']!r}, "
                        f"requested_thread_id={thread_id!r}"
                    )
                record = self._thread_creation_record_from_row(existing)
            else:
                record = self._insert_thread_creation_record(
                    idempotency_key=idempotency_key,
                    initial_state=initial_state,
                    preimage_hash=preimage_hash,
                    graph_binding=graph_binding,
                    capability_profile=capability_profile,
                    created_at=created_at,
                    thread_id=thread_id,
                    delegation_id=delegation_id,
                    task_seed=task_seed,
                    task_reference=task_reference,
                    collaboration_precondition_revision=(
                        collaboration_precondition_revision
                    ),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return record

    def _insert_thread_creation_record(
        self,
        *,
        idempotency_key: str,
        initial_state: str,
        preimage_hash: str,
        graph_binding: str,
        capability_profile: str,
        created_at: datetime,
        thread_id: str | None,
        delegation_id: str | None,
        task_seed: str | None,
        task_reference: str | None,
        collaboration_precondition_revision: int | None,
    ) -> ThreadCreationRecord:
        """插入路径（事务内先验证后写入；调用方已开写事务）。"""
        # owner fence 必须存在且 active：deleting 的 session 禁止新建
        # child 创建 operation（8.1-B 删除流关闭点之后的准入红线）。
        fence_row = self._connection.execute(
            "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
            (_FENCE_ROW_ID,),
        ).fetchone()
        if fence_row is None:
            raise KeyError(
                "session control 缺少 lifecycle fence row，无法冻结 owner "
                f"lifecycle generation: path={self.database_path}"
            )
        if str(fence_row["state"]) != "active":
            raise RuntimeError(
                "owner fence 非 active，拒绝建立 thread creation record"
                f"（fail closed）: path={self.database_path}, "
                f"fence_state={fence_row['state']!r}"
            )
        main_rows = self._connection.execute(
            "SELECT thread_id FROM thread_catalog WHERE kind = 'main'"
        ).fetchall()
        if len(main_rows) != 1:
            raise RuntimeError(
                "session control main row 缺失或多行，拒绝建立 thread "
                f"creation record（fail closed）: path={self.database_path}, "
                f"main_rows={[str(row['thread_id']) for row in main_rows]}"
            )
        main_thread_id = str(main_rows[0]["thread_id"])
        # child ID：调用方传入（已验证 canonical）或软件分配（TODO(identifier):
        # "thr" 前缀待补入 IdentifierPrefix Literal，对齐 session_catalog_store
        # 同款 TODO；create_prefixed_id 基于 uuid4().hex，天然满足 v4 位 profile）。
        child_thread_id = (
            thread_id if thread_id is not None else create_prefixed_id("thr")
        )
        validate_thread_id(child_thread_id)
        if child_thread_id == main_thread_id:
            raise RuntimeError(
                "child thread ID 与 main row thread_id 冲突（fail closed）: "
                f"thread_id={child_thread_id!r}"
            )
        occupied = self._connection.execute(
            "SELECT 1 FROM thread_catalog WHERE thread_id = ?",
            (child_thread_id,),
        ).fetchone()
        if occupied is not None:
            raise RuntimeError(
                "child thread ID 已被 thread catalog 占用（fail closed）: "
                f"thread_id={child_thread_id!r}"
            )
        utc_date = created_at.astimezone(UTC).date()
        final_relative_locator = f"threads/{utc_date:%Y/%m/%d}/{child_thread_id}"
        validate_thread_relative_locator(final_relative_locator)
        locator_taken = self._connection.execute(
            "SELECT 1 FROM thread_creation_records "
            "WHERE final_relative_locator = ?",
            (final_relative_locator,),
        ).fetchone()
        if locator_taken is not None:
            raise RuntimeError(
                "最终 locator 已被其它 thread creation record 冻结"
                f"（fail closed）: final_relative_locator="
                f"{final_relative_locator!r}"
            )
        if delegation_id is not None:
            delegation_taken = self._connection.execute(
                "SELECT 1 FROM thread_creation_records WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if delegation_taken is not None:
                raise RuntimeError(
                    "delegation_id 已绑定其它 thread creation record（部分"
                    "唯一约束，delegated child 一 delegation 一 record）: "
                    f"delegation_id={delegation_id!r}"
                )
        # catalog precondition revision（本轮度量）：thread_catalog 行数。
        # 本轮 thread_catalog 只有插入型变更（行数单调不减），行数等价于
        # 单调 revision；publish 时 CAS 校验行数未漂移。
        catalog_precondition_revision = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog"
            ).fetchone()[0]
        )
        # admission identity（8.5-A 冻结项）：幂等键 + thread ref + 初始
        # state；发布后由幂等 worker create-or-get 初始 execution。
        admission_intent = json.dumps(
            {
                "admission_idempotency_key": idempotency_key,
                "thread_id": child_thread_id,
                "initial_state": initial_state,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record_created_at = datetime.now(UTC).isoformat()
        # 命名参数逐列对应（避免位置占位符错位）：artifact_manifest/
        # artifact_manifest_hash/abort_reason 三列此处为 NULL，由
        # freeze_thread_creation_artifact_manifest 与 abort 流程分别回填。
        self._connection.execute(
            "INSERT INTO thread_creation_records ("
            f"{_THREAD_CREATION_RECORD_COLUMNS}) "
            "VALUES (:idempotency_key, 'preparing', :preimage_hash, "
            ":delegation_id, :child_thread_id, :final_relative_locator, "
            ":staging_locator, NULL, NULL, :graph_binding, "
            ":capability_profile, :task_seed, :task_reference, "
            ":owner_generation, :catalog_revision, :collaboration_revision, "
            ":initial_state, :admission_intent, NULL, :child_created_at, "
            ":record_created_at, :record_updated_at)",
            {
                "idempotency_key": idempotency_key,
                "preimage_hash": preimage_hash,
                "delegation_id": delegation_id,
                "child_thread_id": child_thread_id,
                "final_relative_locator": final_relative_locator,
                "staging_locator": f".staging/{idempotency_key}",
                "graph_binding": graph_binding,
                "capability_profile": capability_profile,
                "task_seed": task_seed,
                "task_reference": task_reference,
                "owner_generation": int(fence_row["generation"]),
                "catalog_revision": catalog_precondition_revision,
                "collaboration_revision": collaboration_precondition_revision,
                "initial_state": initial_state,
                "admission_intent": admission_intent,
                "child_created_at": created_at.isoformat(),
                "record_created_at": record_created_at,
                "record_updated_at": record_created_at,
            },
        )
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            # 防御性兜底：同事务内刚插入必然可见。
            raise RuntimeError(
                "thread creation record 插入后不可见（事务异常）: "
                f"key={idempotency_key!r}"
            )
        return self._thread_creation_record_from_row(row)

    def freeze_thread_creation_artifact_manifest(
        self,
        idempotency_key: str,
        *,
        artifact_manifest: str,
        artifact_manifest_hash: str,
    ) -> ThreadCreationRecord:
        """冻结预期 artifact 内容清单与 hash（preparing record，幂等）。

        - ``artifact_manifest`` 必须是「相对路径 → sha256」映射的
          canonical JSON 文本（store 做形态闸门，service 保证 canonical）；
        - ``artifact_manifest_hash`` 必须是 64 位小写 hex；
        - 已冻结且一致 → 幂等返回既有 record（恢复重入路径）；
        - 已冻结且不一致 → ``RuntimeError``（外部改动或确定性漂移，
          fail closed）；
        - 仅 preparing 可冻结；published/aborted 拒绝。
        """
        self._validate_thread_creation_key(idempotency_key)
        self._validate_frozen_json_text(artifact_manifest, "artifact_manifest")
        parsed = json.loads(artifact_manifest)
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                # 清单映射「内容形态」校验（json.loads 键恒为 str，此处
                # 防御外部篡改），按模块错误分类保持 ValueError。
                raise ValueError(  # noqa: TRY004
                    "artifact_manifest 必须是「相对路径 → sha256 字符串」"
                    f"映射: {key!r} -> {value!r}"
                )
        if (
            not isinstance(artifact_manifest_hash, str)
            or _SHA256_HEX_PATTERN.fullmatch(artifact_manifest_hash) is None
        ):
            raise ValueError(
                "artifact_manifest_hash 必须是 64 位小写 hex: "
                f"{artifact_manifest_hash!r}"
            )
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            if str(row["state"]) != "preparing":
                raise RuntimeError(
                    "thread creation record 非 preparing，拒绝冻结 artifact "
                    f"manifest: key={idempotency_key!r}, state={row['state']!r}"
                )
            frozen_hash = row["artifact_manifest_hash"]
            if frozen_hash is not None:
                if (
                    str(frozen_hash) == artifact_manifest_hash
                    and str(row["artifact_manifest"]) == artifact_manifest
                ):
                    return self._thread_creation_record_from_row(row)
                raise RuntimeError(
                    "thread creation record artifact manifest 已冻结且与重入"
                    "计算不一致（外部改动或确定性漂移，fail closed）: "
                    f"key={idempotency_key!r}, "
                    f"frozen_hash={frozen_hash!r}, "
                    f"requested_hash={artifact_manifest_hash!r}"
                )
            connection.execute(
                "UPDATE thread_creation_records "
                "SET artifact_manifest = ?, artifact_manifest_hash = ?, "
                "record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (
                    artifact_manifest,
                    artifact_manifest_hash,
                    datetime.now(UTC).isoformat(),
                    idempotency_key,
                ),
            )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record 冻结后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def get_thread_creation_record(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """按幂等键返回 ThreadCreationRecord 投影；不存在抛 KeyError。"""
        self._validate_thread_creation_key(idempotency_key)
        self._ensure_open()
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            raise KeyError(
                f"thread creation record 不存在: key={idempotency_key!r}"
            )
        return self._thread_creation_record_from_row(row)

    def get_thread_catalog_revision(self) -> int:
        """返回 catalog precondition revision 的本轮度量：thread_catalog 行数。

        本轮 thread_catalog 只有插入型变更（行数单调不减），行数等价于
        单调 revision；8.5 引入删除/移动时应在同库补建真正的 revision
        计数（TODO(8.5)）。
        """
        self._ensure_open()
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog"
            ).fetchone()[0]
        )

    def publish_thread_creation_record(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """CAS 发布 ThreadCreationRecord（**唯一可见性提交点**，8.5-A）。

        单事务内依次验证：record 存在且 ``state='preparing'``；artifact
        manifest 已冻结；record 内部一致性（child ID canonical、最终
        locator 形态与日期）；**CAS 1**——owner fence 仍为 record 捕获的
        ``(active, generation)``；**CAS 2**——thread catalog 行数等于冻结
        的 catalog precondition revision；collaboration precondition
        revision 非空时 fail closed（collaboration ledger 归 8.5）。随后
        同一事务内插入 ``thread_catalog`` child row（kind='child'）并把
        record 推进为 ``published``。任何失败回滚整个事务：child row 不
        发布、record 保持 preparing（调用方定点清理后 abort）。
        """
        self._validate_thread_creation_key(idempotency_key)
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "thread creation record 已发布，拒绝重复发布: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                raise RuntimeError(
                    "thread creation record 已中止，拒绝发布: "
                    f"key={idempotency_key!r}, "
                    f"abort_reason={row['abort_reason']!r}"
                )
            child_thread_id = str(row["child_thread_id"])
            final_relative_locator = str(row["final_relative_locator"])
            child_created_at_text = str(row["child_created_at"])
            # record 内部一致性复验（防绕过软件直改 record）。
            validate_thread_id(child_thread_id)
            validate_thread_relative_locator(final_relative_locator)
            try:
                child_created_at = datetime.fromisoformat(child_created_at_text)
            except ValueError as error:
                raise RuntimeError(
                    "thread creation record child_created_at 无法解析（record "
                    f"被外部改动，fail closed）: {child_created_at_text!r}: "
                    f"{error}"
                ) from error
            expected_locator = (
                "threads/"
                f"{child_created_at.astimezone(UTC).date():%Y/%m/%d}/"
                f"{child_thread_id}"
            )
            if final_relative_locator != expected_locator:
                raise RuntimeError(
                    "thread creation record 最终 locator 与 child 创建日期不"
                    f"一致（record 被外部改动，fail closed）: "
                    f"key={idempotency_key!r}, "
                    f"frozen={final_relative_locator!r}, "
                    f"expected={expected_locator!r}"
                )
            if row["artifact_manifest_hash"] is None:
                raise RuntimeError(
                    "thread creation record artifact manifest 尚未冻结，拒绝"
                    f"发布（staging 前冻结合同被违反，fail closed）: "
                    f"key={idempotency_key!r}"
                )
            # CAS 1：owner fence 仍为捕获的 active generation。
            fence_row = connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
                (_FENCE_ROW_ID,),
            ).fetchone()
            if fence_row is None:
                raise KeyError(
                    "session control 缺少 lifecycle fence row: "
                    f"path={self.database_path}"
                )
            expected_generation = int(row["owner_session_lifecycle_generation"])
            if (
                str(fence_row["state"]) != "active"
                or int(fence_row["generation"]) != expected_generation
            ):
                raise RuntimeError(
                    "thread creation publish CAS 失败：owner fence 已漂移: "
                    f"key={idempotency_key!r}, "
                    f"expected=(active, {expected_generation}), "
                    f"actual=({fence_row['state']!r}, "
                    f"{int(fence_row['generation'])})"
                )
            # CAS 2：catalog precondition revision 未漂移（本轮度量=行数）。
            actual_revision = int(
                connection.execute(
                    "SELECT COUNT(*) FROM thread_catalog"
                ).fetchone()[0]
            )
            frozen_revision = int(row["catalog_precondition_revision"])
            if actual_revision != frozen_revision:
                raise RuntimeError(
                    "thread creation publish CAS 失败：thread catalog "
                    f"precondition revision 已漂移: key={idempotency_key!r}, "
                    f"expected_revision={frozen_revision}, "
                    f"actual_revision={actual_revision}"
                )
            if row["collaboration_precondition_revision"] is not None:
                raise RuntimeError(
                    "thread creation record 冻结了非空 collaboration "
                    "precondition revision，但 collaboration ledger 尚未在本"
                    f"库落地（8.5），无法校验（fail closed）: "
                    f"key={idempotency_key!r}, "
                    f"frozen={row['collaboration_precondition_revision']!r}"
                )
            # 唯一可见性提交点前的最后占用预检（UNIQUE 兜底）。
            occupied = connection.execute(
                "SELECT 1 FROM thread_catalog WHERE thread_id = ?",
                (child_thread_id,),
            ).fetchone()
            if occupied is not None:
                raise RuntimeError(
                    "thread creation publish 失败：child thread 已存在于 "
                    f"thread catalog（fail closed）: "
                    f"thread_id={child_thread_id!r}"
                )
            # 唯一可见性提交点：thread_catalog child row 插入 + record
            # → published 同一事务；record 状态变化本身不构成可见性。
            connection.execute(
                "INSERT INTO thread_catalog (thread_id, kind, created_at) "
                "VALUES (?, 'child', ?)",
                (child_thread_id, child_created_at_text),
            )
            connection.execute(
                "UPDATE thread_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record 发布后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def abort_thread_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> ThreadCreationRecord:
        """终结 ThreadCreationRecord：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError，8.5-A：CAS 失败不发布、
        已发布不回退）；已 aborted 幂等返回既有 record（不覆盖原
        abort_reason）。
        """
        self._validate_thread_creation_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "thread creation record 已发布，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._thread_creation_record_from_row(row)
            connection.execute(
                "UPDATE thread_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def mark_thread_creation_published(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """published 终态的幂等校验返回（发布后/terminal 响应前崩溃恢复）。

        - record ``published`` → 复验 thread_catalog child row 仍存在
          （唯一可见性提交点的产物），随后幂等返回既有 record；
        - record ``preparing`` → ``RuntimeError``（必须经
          :meth:`publish_thread_creation_record` 推进，不得绕过 CAS）；
        - record ``aborted`` → ``RuntimeError``；
        - record 缺失 → ``KeyError``。
        """
        self._validate_thread_creation_key(idempotency_key)
        self._ensure_open()
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            raise KeyError(
                f"thread creation record 不存在: key={idempotency_key!r}"
            )
        state = str(row["state"])
        if state == "preparing":
            raise RuntimeError(
                "thread creation record 仍 preparing，published 终态必须经 "
                f"publish_thread_creation_record 推进: key={idempotency_key!r}"
            )
        if state == "aborted":
            raise RuntimeError(
                "thread creation record 已中止，不是 published 终态: "
                f"key={idempotency_key!r}, abort_reason={row['abort_reason']!r}"
            )
        visible = self._connection.execute(
            "SELECT 1 FROM thread_catalog WHERE thread_id = ?",
            (str(row["child_thread_id"]),),
        ).fetchone()
        if visible is None:
            raise RuntimeError(
                "thread creation record 已 published 但 thread_catalog child "
                "row 缺失（唯一可见性提交点产物被外部改动，fail closed）: "
                f"key={idempotency_key!r}, "
                f"child_thread_id={row['child_thread_id']!r}"
            )
        return self._thread_creation_record_from_row(row)

    # ------------------------------------------------------------------
    # 初始 execution admission intent（8.5-A 接口契约，R20 只落库不绑定 Job）
    # ------------------------------------------------------------------

    @staticmethod
    def _thread_execution_intent_from_row(
        row: sqlite3.Row,
    ) -> ThreadExecutionIntent:
        return ThreadExecutionIntent(
            admission_idempotency_key=str(row["admission_idempotency_key"]),
            session_id=str(row["session_id"]),
            thread_id=str(row["thread_id"]),
            creation_idempotency_key=str(row["creation_idempotency_key"]),
            initial_state=str(row["initial_state"]),
            state=str(row["state"]),
            intent_created_at=str(row["intent_created_at"]),
            intent_updated_at=str(row["intent_updated_at"]),
        )

    def create_or_get_initial_execution_intent(
        self,
        *,
        admission_idempotency_key: str,
        session_id: str,
        thread_id: str,
        initial_state: str,
        creation_idempotency_key: str,
    ) -> ThreadExecutionIntent:
        """create-or-get 初始 execution 的持久 admission intent（幂等 worker
        接口契约，8.5 实现 Job 绑定时消费）。

        前置校验（fail closed）：对应 thread creation record 必须已
        ``published`` 且 child ID/initial_state 与入参一致；thread_catalog
        child row 必须可见（唯一可见性提交点已过）。同 admission key 幂等
        返回既有 intent（thread_id/initial_state/creation key 一致）；
        不一致冲突拒绝；同 thread 不同 admission key 由
        ``idx_thread_execution_intent_thread`` 唯一索引拒绝（崩溃不能留下
        重复初始 Job 的持久侧入口）。本轮只写 ``state='pending'``，不绑定
        真实 Job（8.5）。
        """
        self._validate_thread_creation_key(admission_idempotency_key)
        self._validate_thread_creation_key(creation_idempotency_key)
        validate_session_id(session_id)
        validate_thread_id(thread_id)
        if initial_state not in _INITIAL_STATE_VALUES:
            raise ValueError(f"initial_state 非法: {initial_state!r}")
        with self._write_transaction() as connection:
            record_row = self._fetch_thread_creation_record(
                connection, creation_idempotency_key
            )
            if record_row is None:
                raise KeyError(
                    "thread creation record 不存在，无法建立初始 execution "
                    f"intent: creation_key={creation_idempotency_key!r}"
                )
            if str(record_row["state"]) != "published":
                raise RuntimeError(
                    "thread creation record 尚未 published，拒绝建立初始 "
                    f"execution intent: creation_key="
                    f"{creation_idempotency_key!r}, state={record_row['state']!r}"
                )
            if str(record_row["child_thread_id"]) != thread_id:
                raise RuntimeError(
                    "initial execution intent thread_id 与 creation record "
                    f"冻结 child 不一致（fail closed）: thread_id={thread_id!r}, "
                    f"record_child={record_row['child_thread_id']!r}"
                )
            if str(record_row["initial_state"]) != initial_state:
                raise RuntimeError(
                    "initial execution intent initial_state 与 creation "
                    f"record 冻结值不一致（fail closed）: "
                    f"requested={initial_state!r}, "
                    f"frozen={record_row['initial_state']!r}"
                )
            visible = connection.execute(
                "SELECT 1 FROM thread_catalog WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if visible is None:
                raise RuntimeError(
                    "child thread 尚未出现在 thread catalog（唯一可见性提交"
                    f"点未过），拒绝建立初始 execution intent: "
                    f"thread_id={thread_id!r}"
                )
            now_text = datetime.now(UTC).isoformat()
            existing = connection.execute(
                f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
                "FROM thread_execution_intents "
                "WHERE admission_idempotency_key = ?",
                (admission_idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["thread_id"]) != thread_id
                    or str(existing["initial_state"]) != initial_state
                    or str(existing["creation_idempotency_key"])
                    != creation_idempotency_key
                    or str(existing["session_id"]) != session_id
                ):
                    raise RuntimeError(
                        "initial execution intent 幂等冲突（同 admission key "
                        f"不同身份，拒绝复用）: admission_key="
                        f"{admission_idempotency_key!r}, "
                        f"existing_thread_id={existing['thread_id']!r}"
                    )
                return self._thread_execution_intent_from_row(existing)
            duplicate_thread = connection.execute(
                "SELECT 1 FROM thread_execution_intents WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if duplicate_thread is not None:
                raise RuntimeError(
                    "该 child thread 已存在初始 execution intent（唯一索引，"
                    f"拒绝第二个 admission key）: thread_id={thread_id!r}"
                )
            connection.execute(
                f"INSERT INTO thread_execution_intents ({_THREAD_EXECUTION_INTENT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    admission_idempotency_key,
                    session_id,
                    thread_id,
                    creation_idempotency_key,
                    initial_state,
                    now_text,
                    now_text,
                ),
            )
            inserted = connection.execute(
                f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
                "FROM thread_execution_intents "
                "WHERE admission_idempotency_key = ?",
                (admission_idempotency_key,),
            ).fetchone()
            if inserted is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "initial execution intent 插入后不可见（事务异常）: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            return self._thread_execution_intent_from_row(inserted)

    def get_initial_execution_intent(
        self,
        admission_idempotency_key: str,
    ) -> ThreadExecutionIntent:
        """按 admission 幂等键返回 intent 投影；不存在抛 KeyError。"""
        self._validate_thread_creation_key(admission_idempotency_key)
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
            "FROM thread_execution_intents "
            "WHERE admission_idempotency_key = ?",
            (admission_idempotency_key,),
        ).fetchone()
        if row is None:
            raise KeyError(
                "initial execution intent 不存在: "
                f"admission_key={admission_idempotency_key!r}"
            )
        return self._thread_execution_intent_from_row(row)
