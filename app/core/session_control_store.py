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

**8.5-B 扩展（R23 已落地）**：``thread_execution_intents`` 升级为消费
形态（SCHEMA_VERSION 2→3，单事务加法升级）：冻结软件生成的稳定
``execution_binding_id`` / ``job_id``（由 admission 幂等键确定性派生，
重试不变）、``binding_preimage_hash``（至少覆盖 admission key、owner
session、thread、creation key、initial state 与稳定 binding/job
identity）、claim owner/generation 可恢复领取字段与显式
``last_error``（不按 TTL 自动丢弃 intent）；v2 pending 行迁移时确定性
补齐稳定 identity。新增只读/事务 API：``list_pending_initial_execution_intents``
（纯状态索引，不扫目录）、``claim_initial_execution_intent``（同一
admission 只允许一个有效 claim；相同 claim 幂等，不同 claim 冲突 fail
closed）、``mark_initial_execution_bound``（校验完整 preimage、claim
与稳定 job/binding identity 后 CAS ``pending -> bound``，重复相同提交
幂等）、``record_initial_execution_failure``（只记录明确错误并保留
pending 可恢复事实，不宣称 bound）。
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

import hashlib
import json
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
from app.core.session_control_communication_ledger.communication_ledger import (
    COMMUNICATION_INBOX_TABLE_DDL,
    COMMUNICATION_OUTBOX_TABLE_DDL,
    IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL,
    CommunicationInboxRecord,
    CommunicationLedgerMixin,
    CommunicationOutboxRecord,
    derive_communication_admission_identity,
)
from app.core.session_control_operation_lease.operation_lease import (
    IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL,
    SESSION_OPERATION_LEASES_TABLE_DDL,
    OperationLeaseMixin,
)
from app.core.session_control_primitives import (
    EXECUTION_BINDING_ID_PATTERN,
    EXECUTION_JOB_ID_PATTERN,
    SHA256_HEX_PATTERN,
    validate_claim_fields,
    validate_thread_creation_key,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    LIFECYCLE_FENCE_TABLE_DDL,
    THREAD_CATALOG_TABLE_DDL,
    ThreadCatalogMixin,
    fetch_fence_row,
    read_fence_row,
    validate_thread_relative_locator,
)
from app.core.session_control_thread_owner_binding.thread_owner_binding import (
    THREAD_OWNER_BINDINGS_TABLE_DDL,
    ThreadOwnerBinding,
    ThreadOwnerBindingMixin,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS

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

# collaboration ledger（8.5-B，R25）：owner session 内 delegation/member
# 的权威协作账本。revision 是账本单调版本（member 登记唯一推进点），
# thread creation record 冻结 collaboration_precondition_revision 并在
# publish 时 CAS 校验未漂移；member 行随 thread catalog publish 在同一
# 事务内 registering → published（原子可见性）。
_COLLABORATION_LEDGER_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_ledger (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
)
"""

_COLLABORATION_MEMBERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS collaboration_members (
    delegation_id TEXT PRIMARY KEY,
    coordinator_session_id TEXT NOT NULL,
    coordinator_thread_id TEXT NOT NULL,
    child_thread_id TEXT,
    role TEXT NOT NULL,
    subagent_type TEXT NOT NULL,
    title TEXT NOT NULL,
    task_seed TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('registering', 'published', 'cancelled')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_THREAD_CREATION_RECORD_COLUMNS = (
    "thread_creation_idempotency_key, state, preimage_hash, delegation_id, "
    "child_thread_id, final_relative_locator, staging_locator, "
    "artifact_manifest, artifact_manifest_hash, graph_binding, "
    "capability_profile, task_seed, task_reference, "
    "owner_session_lifecycle_generation, catalog_precondition_revision, "
    "collaboration_precondition_revision, initial_state, admission_intent, "
    "abort_reason, child_created_at, record_created_at, record_updated_at"
)

# 初始 execution 的持久 admission intent（8.5-A 落库 + 8.5-B 消费状态机，
# R23）：``execution_binding_id``/``job_id`` 由 admission 幂等键确定性
# 派生（软件生成、重试不变）；``binding_preimage_hash`` 覆盖 admission
# key、owner session、thread、creation key、initial state 与稳定
# binding/job identity；claim owner/generation 是可恢复领取字段（不按
# TTL 丢弃）；``last_error`` 只记录明确错误并保留 pending 可恢复事实。
_THREAD_EXECUTION_INTENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_execution_intents (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'bound')),
    execution_binding_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL UNIQUE,
    binding_preimage_hash TEXT NOT NULL,
    claim_owner TEXT,
    claim_generation INTEGER,
    last_error TEXT,
    intent_created_at TEXT NOT NULL,
    intent_updated_at TEXT NOT NULL
)
"""

# v2→v3 升级临时表（普通 CREATE，不带 IF NOT EXISTS；升级完成后 RENAME
# 回 ``thread_execution_intents``，任何失败随 ``_initialize`` 事务整体
# 回滚）。
_THREAD_EXECUTION_INTENTS_V3_UPGRADE_TABLE_DDL = """
CREATE TABLE thread_execution_intents_v3_upgrade (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'bound')),
    execution_binding_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL UNIQUE,
    binding_preimage_hash TEXT NOT NULL,
    claim_owner TEXT,
    claim_generation INTEGER,
    last_error TEXT,
    intent_created_at TEXT NOT NULL,
    intent_updated_at TEXT NOT NULL,
    CHECK ((claim_owner IS NULL) = (claim_generation IS NULL)),
    CHECK (claim_generation IS NULL OR claim_generation >= 1)
)
"""

# 每个 child thread 至多一条初始 execution intent（崩溃不能留下重复初始
# Job 的持久侧保证）。
_IDX_THREAD_EXECUTION_INTENT_THREAD_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_execution_intent_thread "
    "ON thread_execution_intents(thread_id)"
)

# worker 只消费状态索引（8.5-B）：pending 列表查询的覆盖索引。
_IDX_THREAD_EXECUTION_INTENT_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_thread_execution_intent_state "
    "ON thread_execution_intents(state)"
)

_THREAD_EXECUTION_INTENT_COLUMNS = (
    "admission_idempotency_key, session_id, thread_id, "
    "creation_idempotency_key, initial_state, state, "
    "execution_binding_id, job_id, binding_preimage_hash, "
    "claim_owner, claim_generation, last_error, "
    "intent_created_at, intent_updated_at"
)

# v2 表列清单（仅 v2→v3 升级路径读取旧表使用）。
_THREAD_EXECUTION_INTENT_V2_COLUMNS = (
    "admission_idempotency_key, session_id, thread_id, "
    "creation_idempotency_key, initial_state, state, "
    "intent_created_at, intent_updated_at"
)

_INITIAL_STATE_VALUES = ("running", "idle")

# collaboration member 状态闭集（8.5-B ledger）：registering（登记未发布）、
# published（随 thread catalog publish 原子转正）、cancelled（record abort
# 定点取消；同 delegation 不换绑，重试必须换新 delegation）。
_COLLABORATION_MEMBER_STATES = ("registering", "published", "cancelled")

_COLLABORATION_MEMBER_COLUMNS = (
    "delegation_id, coordinator_session_id, coordinator_thread_id, "
    "child_thread_id, role, subagent_type, title, task_seed, state, "
    "registered_at, updated_at"
)


def derive_initial_execution_identity(
    admission_idempotency_key: str,
) -> tuple[str, str]:
    """由 admission 幂等键确定性派生 ``(execution_binding_id, job_id)``。

    二者均由软件生成且重试不变：以 admission key（admission 唯一）为
    唯一熵源做 sha256 截断派生，不依赖当前时间、随机数或进程状态——同
    库 v2 行升级与全新 create-or-get 在任意进程、任意时刻重复执行都得
    到同一 identity。
    """
    if not isinstance(admission_idempotency_key, str):
        raise TypeError(
            "admission_idempotency_key 必须是字符串: "
            f"{admission_idempotency_key!r}"
        )
    binding_digest = hashlib.sha256(
        f"initial-execution-binding\x00{admission_idempotency_key}".encode()
    ).hexdigest()[:32]
    job_digest = hashlib.sha256(
        f"initial-execution-job\x00{admission_idempotency_key}".encode()
    ).hexdigest()[:32]
    return f"tbind_{binding_digest}", f"job_{job_digest}"


def compute_initial_execution_binding_preimage_hash(
    *,
    admission_idempotency_key: str,
    session_id: str,
    thread_id: str,
    creation_idempotency_key: str,
    initial_state: str,
    execution_binding_id: str,
    job_id: str,
) -> str:
    """计算 binding preimage hash（canonical JSON 的 sha256 小写 hex）。

    preimage 至少覆盖任务书 §2 要求的面：admission key、owner session、
    thread、creation key、initial state、稳定 binding/job identity。
    store 在 intent 落库与 ``mark_initial_execution_bound`` 时用同一
    口径复算，任何 identity 漂移都会得到不同 hash（fail closed）。
    """
    preimage = json.dumps(
        {
            "admission_idempotency_key": admission_idempotency_key,
            "session_id": session_id,
            "thread_id": thread_id,
            "creation_idempotency_key": creation_idempotency_key,
            "initial_state": initial_state,
            "execution_binding_id": execution_binding_id,
            "job_id": job_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _validate_execution_identity(
    execution_binding_id: str,
    job_id: str,
) -> None:
    """校验稳定 binding/job identity 形态（固定前缀 + 32 位小写 hex）。"""
    if not isinstance(execution_binding_id, str):
        raise TypeError(
            f"execution_binding_id 必须是字符串: {execution_binding_id!r}"
        )
    if EXECUTION_BINDING_ID_PATTERN.fullmatch(execution_binding_id) is None:
        raise ValueError(
            f"execution_binding_id 形态非法: {execution_binding_id!r}"
        )
    if not isinstance(job_id, str):
        raise TypeError(f"job_id 必须是字符串: {job_id!r}")
    if EXECUTION_JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError(f"job_id 形态非法: {job_id!r}")


def _fetch_execution_intent_row(
    connection: sqlite3.Connection,
    admission_idempotency_key: str,
) -> sqlite3.Row | None:
    """按 admission 幂等键取 intent 行投影；缺失返回 None。

    intent 的判态/回读（create-or-get、claim、mark bound、record
    failure 与单条读取）共用本查询，避免同一 SELECT 在多处复制；各调用
    点仍各自决定缺失时的错误文案与分支。
    """
    return connection.execute(
        f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
        "FROM thread_execution_intents "
        "WHERE admission_idempotency_key = ?",
        (admission_idempotency_key,),
    ).fetchone()


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

    ``state`` 闭集为 ``pending/bound``；创建流只写 ``pending``，
    ``bound`` 由 8.5-B worker 经 claim + CAS 推进。``admission_idempotency_key``
    是 worker 幂等键（创建流以 creation idempotency key 派生），
    ``thread_id`` 上有唯一索引——同一 child thread 至多一条初始 execution
    intent，崩溃不会留下重复初始 Job 的持久侧入口。
    ``execution_binding_id``/``job_id`` 是软件生成、重试不变的稳定
    identity（由 admission key 确定性派生）；``binding_preimage_hash``
    覆盖 admission key、owner session、thread、creation key、initial
    state 与稳定 binding/job identity；``claim_owner``/``claim_generation``
    是可恢复领取字段（无 TTL、不自动丢弃）；``last_error`` 只记录
    明确错误，state 保持 ``pending`` 可恢复。
    """

    admission_idempotency_key: str
    session_id: str
    thread_id: str
    creation_idempotency_key: str
    initial_state: str
    state: str
    execution_binding_id: str
    job_id: str
    binding_preimage_hash: str
    claim_owner: str | None
    claim_generation: int | None
    last_error: str | None
    intent_created_at: str
    intent_updated_at: str


@dataclass(frozen=True, slots=True)
class CollaborationMember:
    """collaboration_members 表行的不可变投影（8.5-B 协作账本）。

    ``state`` 闭集为 ``registering/published/cancelled``；member 随
    thread catalog publish 在同一事务内转正（``child_thread_id`` 回填，
    原子可见性）；record abort 定点取消且同 delegation 不换绑。
    """

    delegation_id: str
    coordinator_session_id: str
    coordinator_thread_id: str
    child_thread_id: str | None
    role: str
    subagent_type: str
    title: str
    task_seed: str
    state: str
    registered_at: str
    updated_at: str


class SessionControlStore(
    CommunicationLedgerMixin,
    OperationLeaseMixin,
    ThreadCatalogMixin,
    ThreadOwnerBindingMixin,
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
        后原样抛出——模式对齐 ``session_catalog_store._write_transaction``，
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

    def _initialize(self) -> None:
        """幂等建表并设置 user_version；未知版本 fail-closed 拒绝打开。

        支持的 current：0（全新库，建全部表并置 v3）、1（R12/R13 v1 库，
        同一事务内先以「建临时新表→拷贝→校验→删旧→改名」升级
        ``thread_catalog`` 的 kind CHECK、再幂等补建 8.5-B 新表并升
        v3，既有 main row 数据零丢失）、2（R20 v2 库，同一事务内以
        「建临时新表→确定性拷贝→校验→删旧→改名」升级
        ``thread_execution_intents`` 补齐稳定 binding/job identity）、
        3/4/5（历史版本，幂等补建新表后推进）、6（当前版本；全部 DDL
        均为幂等 no-op）。
        """
        current = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current not in (0, 1, 2, 3, 4, 5, 6, self.SCHEMA_VERSION):
            raise RuntimeError(
                "session control schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        self._begin_immediate()
        try:
            if current == 1:
                self._upgrade_thread_catalog_kind_v1_to_v2()
            if current == 2:
                self._upgrade_thread_execution_intents_v2_to_v3()
            self._connection.execute(THREAD_CATALOG_TABLE_DDL)
            self._connection.execute(LIFECYCLE_FENCE_TABLE_DDL)
            self._connection.execute(_THREAD_CREATION_RECORDS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_CREATION_DELEGATION_DDL)
            self._connection.execute(_COLLABORATION_LEDGER_TABLE_DDL)
            self._connection.execute(
                "INSERT INTO collaboration_ledger (id, revision) "
                "VALUES (1, 0) ON CONFLICT(id) DO NOTHING"
            )
            self._connection.execute(_COLLABORATION_MEMBERS_TABLE_DDL)
            self._connection.execute(_THREAD_EXECUTION_INTENTS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_EXECUTION_INTENT_THREAD_DDL)
            self._connection.execute(_IDX_THREAD_EXECUTION_INTENT_STATE_DDL)
            self._connection.execute(SESSION_OPERATION_LEASES_TABLE_DDL)
            self._connection.execute(
                IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL
            )
            self._connection.execute(THREAD_OWNER_BINDINGS_TABLE_DDL)
            self._connection.execute(COMMUNICATION_OUTBOX_TABLE_DDL)
            self._connection.execute(COMMUNICATION_INBOX_TABLE_DDL)
            self._connection.execute(IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL)
            if current != self.SCHEMA_VERSION:
                self._connection.execute(
                    f"PRAGMA user_version = {self.SCHEMA_VERSION}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _upgrade_thread_execution_intents_v2_to_v3(self) -> None:
        """v2→v3 加法升级：``thread_execution_intents`` 重建补齐稳定 identity。

        任务书 §2.1：v2 pending 行迁移时确定性补齐软件生成的稳定
        binding/job identity 与 preimage hash——identity 只由行内
        admission 幂等键派生（:func:`derive_initial_execution_identity`），
        不依赖当前时间或随机数，同库任意进程重复执行结果一致；preimage
        hash 用与全新落库相同的口径复算。claim 字段迁移为 NULL（v2 无
        消费语义）、``last_error`` 迁移为 NULL；``state`` 非
        ``pending`` 的行在 v2 从未被合法写入（v2 没有 bound 推进接口），
        fail closed 拒绝迁移。单事务内「建临时新表→确定性拷贝→行数校验
        →删旧→改名」，任何失败随 ``_initialize`` 事务整体回滚
        （``user_version`` 保持 2，库可原样重开）。
        """
        self._connection.execute(
            _THREAD_EXECUTION_INTENTS_V3_UPGRADE_TABLE_DDL
        )
        rows = self._connection.execute(
            f"SELECT {_THREAD_EXECUTION_INTENT_V2_COLUMNS} "
            "FROM thread_execution_intents "
            "ORDER BY admission_idempotency_key"
        ).fetchall()
        for row in rows:
            state = str(row["state"])
            if state != "pending":
                raise RuntimeError(
                    "thread_execution_intents v2→v3 升级发现非 pending 行"
                    "（v2 从未提供 bound 推进接口，库被外部改动，fail "
                    "closed）: admission_key="
                    f"{row['admission_idempotency_key']!r}, state={state!r}"
                )
            admission_key = str(row["admission_idempotency_key"])
            session_id = str(row["session_id"])
            thread_id = str(row["thread_id"])
            creation_key = str(row["creation_idempotency_key"])
            initial_state = str(row["initial_state"])
            # 行级身份复验（防绕过软件直改 v2 库）：与落库闸门同口径。
            validate_session_id(session_id)
            validate_thread_id(thread_id)
            validate_thread_creation_key(admission_key)
            validate_thread_creation_key(creation_key)
            if initial_state not in _INITIAL_STATE_VALUES:
                raise RuntimeError(
                    "thread_execution_intents v2→v3 升级发现非法 "
                    f"initial_state 行（fail closed）: admission_key="
                    f"{admission_key!r}, initial_state={initial_state!r}"
                )
            binding_id, job_id = derive_initial_execution_identity(
                admission_key
            )
            preimage_hash = compute_initial_execution_binding_preimage_hash(
                admission_idempotency_key=admission_key,
                session_id=session_id,
                thread_id=thread_id,
                creation_idempotency_key=creation_key,
                initial_state=initial_state,
                execution_binding_id=binding_id,
                job_id=job_id,
            )
            self._connection.execute(
                "INSERT INTO thread_execution_intents_v3_upgrade "
                f"({_THREAD_EXECUTION_INTENT_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    admission_key,
                    session_id,
                    thread_id,
                    creation_key,
                    initial_state,
                    binding_id,
                    job_id,
                    preimage_hash,
                    str(row["intent_created_at"]),
                    str(row["intent_updated_at"]),
                ),
            )
        before = len(rows)
        after = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_execution_intents_v3_upgrade"
            ).fetchone()[0]
        )
        if before != after:
            raise RuntimeError(
                "thread_execution_intents v2→v3 升级拷贝行数不一致（数据零"
                f"丢失保证被破坏，事务将回滚）: path={self.database_path}, "
                f"before={before}, after={after}"
            )
        self._connection.execute("DROP TABLE thread_execution_intents")
        self._connection.execute(
            "ALTER TABLE thread_execution_intents_v3_upgrade "
            "RENAME TO thread_execution_intents"
        )

    # ------------------------------------------------------------------
    # collaboration ledger（8.5-B，R25：delegation/member 权威账本）
    # ------------------------------------------------------------------

    @staticmethod
    def _collaboration_member_from_row(row: sqlite3.Row) -> CollaborationMember:
        return CollaborationMember(
            delegation_id=str(row["delegation_id"]),
            coordinator_session_id=str(row["coordinator_session_id"]),
            coordinator_thread_id=str(row["coordinator_thread_id"]),
            child_thread_id=(
                str(row["child_thread_id"])
                if row["child_thread_id"] is not None
                else None
            ),
            role=str(row["role"]),
            subagent_type=str(row["subagent_type"]),
            title=str(row["title"]),
            task_seed=str(row["task_seed"]),
            state=str(row["state"]),
            registered_at=str(row["registered_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_collaboration_ledger_revision(self) -> int:
        """返回 collaboration ledger 当前 revision（member 登记推进）。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT revision FROM collaboration_ledger WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "collaboration ledger row 缺失（库被外部改动，fail closed）: "
                f"path={self.database_path}"
            )
        return int(row["revision"])

    def register_collaboration_member(
        self,
        *,
        delegation_id: str,
        coordinator_session_id: str,
        coordinator_thread_id: str,
        role: str,
        subagent_type: str,
        title: str,
        task_seed: str,
    ) -> int:
        """create-or-get collaboration member（幂等；返回当前 ledger revision）。

        - 新 delegation → 插入 ``registering`` member 并把 ledger
          revision +1（revision 的唯一推进点）；
        - 同 delegation 同内容重入 → 幂等 no-op（revision 不变；published
          member 重入同样幂等，供 publish 后/terminal 前崩溃恢复）；
        - 同 delegation 内容漂移或已 ``cancelled``（同 delegation 不换
          绑）→ ``RuntimeError`` fail closed。
        """
        validate_thread_creation_key(delegation_id)
        for name, value in (
            ("coordinator_session_id", coordinator_session_id),
            ("coordinator_thread_id", coordinator_thread_id),
            ("role", role),
            ("subagent_type", subagent_type),
            ("title", title),
            ("task_seed", task_seed),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} 不能为空: {value!r}")
        with self._write_transaction() as connection:
            existing = connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["coordinator_session_id"])
                    != coordinator_session_id
                    or str(existing["coordinator_thread_id"])
                    != coordinator_thread_id
                    or str(existing["role"]) != role
                    or str(existing["subagent_type"]) != subagent_type
                    or str(existing["title"]) != title
                    or str(existing["task_seed"]) != task_seed
                ):
                    raise RuntimeError(
                        "collaboration member 幂等冲突（同 delegation 不同"
                        f"内容，拒绝换绑）: delegation_id={delegation_id!r}"
                    )
                if str(existing["state"]) == "cancelled":
                    raise RuntimeError(
                        "collaboration member 已取消，同 delegation 不换绑"
                        "（fail closed，重试须换新 delegation）: "
                        f"delegation_id={delegation_id!r}"
                    )
                return self.get_collaboration_ledger_revision()
            connection.execute(
                f"INSERT INTO collaboration_members "
                f"({_COLLABORATION_MEMBER_COLUMNS}) VALUES "
                "(?, ?, ?, NULL, ?, ?, ?, ?, 'registering', ?, ?)",
                (
                    delegation_id,
                    coordinator_session_id,
                    coordinator_thread_id,
                    role,
                    subagent_type,
                    title,
                    task_seed,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                "UPDATE collaboration_ledger SET revision = revision + 1 "
                "WHERE id = 1"
            )
            return self.get_collaboration_ledger_revision()

    def get_collaboration_member(self, delegation_id: str) -> CollaborationMember:
        """按 delegation 幂等键返回 member 投影；不存在抛 KeyError。"""
        validate_thread_creation_key(delegation_id)
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
            "FROM collaboration_members WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"collaboration member 不存在: delegation_id={delegation_id!r}"
            )
        return self._collaboration_member_from_row(row)

    def list_collaboration_members(
        self,
        *,
        coordinator_session_id: str | None = None,
    ) -> tuple[CollaborationMember, ...]:
        """列出 member（可选按 coordinator session 过滤；确定性排序）。"""
        self._ensure_open()
        if coordinator_session_id is None:
            rows = self._connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members "
                "ORDER BY registered_at, delegation_id"
            ).fetchall()
        else:
            validate_session_id(coordinator_session_id)
            rows = self._connection.execute(
                f"SELECT {_COLLABORATION_MEMBER_COLUMNS} "
                "FROM collaboration_members "
                "WHERE coordinator_session_id = ? "
                "ORDER BY registered_at, delegation_id",
                (coordinator_session_id,),
            ).fetchall()
        return tuple(
            self._collaboration_member_from_row(row) for row in rows
        )

    # ------------------------------------------------------------------
    # ThreadCreationRecord（8.5-A，R20：child thread 创建流 operation lease）
    # ------------------------------------------------------------------

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
        validate_thread_creation_key(idempotency_key)
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
        self._begin_immediate()
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
        fence_row = fetch_fence_row(self._connection)
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
        validate_thread_creation_key(idempotency_key)
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
            or SHA256_HEX_PATTERN.fullmatch(artifact_manifest_hash) is None
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
        validate_thread_creation_key(idempotency_key)
        self._ensure_open()
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            raise KeyError(
                f"thread creation record 不存在: key={idempotency_key!r}"
            )
        return self._thread_creation_record_from_row(row)

    def publish_thread_creation_record(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """CAS 发布 ThreadCreationRecord（**唯一可见性提交点**，8.5-A）。

        单事务内依次验证：record 存在且 ``state='preparing'``；artifact
        manifest 已冻结；record 内部一致性（child ID canonical、最终
        locator 形态与日期）；**CAS 1**——owner fence 仍为 record 捕获的
        ``(active, generation)``；**CAS 2**——thread catalog 行数不得小于冻结
        的 catalog precondition revision（收缩=外部改动 fail closed；并发
        sibling publish 的合法增长不拦截，2.3-A 合同）；collaboration precondition
        revision 非空时 fail closed（collaboration ledger 归 8.5）。随后
        同一事务内插入 ``thread_catalog`` child row（kind='child'）并把
        record 推进为 ``published``。任何失败回滚整个事务：child row 不
        发布、record 保持 preparing（调用方定点清理后 abort）。
        """
        validate_thread_creation_key(idempotency_key)
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
            fence_row = read_fence_row(
                connection, database_path=self.database_path
            )
            expected_generation = int(row["owner_session_lifecycle_generation"])
            if str(fence_row["state"]) == "deleting":
                # 2.3-D：local fence 已 deleting → 统一删除中错误合同。
                raise SessionDeletionPendingError(
                    "session_deletion_pending: owner fence 已 deleting，"
                    f"拒绝新可见性发布（fail closed）: key={idempotency_key!r}"
                )
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
            # CAS 2：catalog precondition revision 收缩检测（度量=行数）。
            # 2.3-A 合同：同 Session 并发 sibling child 创建（不同幂等键）
            # 是合法交错，行数会因 sibling publish 合法增长；行数收缩只可
            # 能来自外部直改（删除流会先关 fence，CAS 1 已拦截）。故
            # frozen > actual 才 fail closed；精确相等会误伤合法并发发布
            # （跨进程文件锁使该交错成为真实合同而非偶然串行化产物）。
            actual_revision = int(
                connection.execute(
                    "SELECT COUNT(*) FROM thread_catalog"
                ).fetchone()[0]
            )
            frozen_revision = int(row["catalog_precondition_revision"])
            if actual_revision < frozen_revision:
                raise RuntimeError(
                    "thread creation publish CAS 失败：thread catalog "
                    f"precondition revision 已回退（行数收缩=外部改动，"
                    f"fail closed）: key={idempotency_key!r}, "
                    f"frozen_revision={frozen_revision}, "
                    f"actual_revision={actual_revision}"
                )
            # CAS 3：collaboration precondition revision 未漂移（R25 起
            # ledger 在本库落地；delegated record 必须冻结非空 revision，
            # manual creation（无 delegation）保持 None 且跳过校验）。
            if row["delegation_id"] is not None and (
                row["collaboration_precondition_revision"] is None
            ):
                raise RuntimeError(
                    "delegated thread creation record 缺少 collaboration "
                    f"precondition revision（fail closed）: "
                    f"key={idempotency_key!r}"
                )
            if row["collaboration_precondition_revision"] is not None:
                actual_collaboration_revision = int(
                    connection.execute(
                        "SELECT revision FROM collaboration_ledger "
                        "WHERE id = 1"
                    ).fetchone()[0]
                )
                frozen_collaboration_revision = int(
                    row["collaboration_precondition_revision"]
                )
                if (
                    actual_collaboration_revision
                    != frozen_collaboration_revision
                ):
                    raise RuntimeError(
                        "thread creation publish CAS 失败：collaboration "
                        "ledger revision 已漂移: "
                        f"key={idempotency_key!r}, "
                        f"expected_revision={frozen_collaboration_revision}, "
                        f"actual_revision={actual_collaboration_revision}"
                    )
                member_row = connection.execute(
                    "SELECT state FROM collaboration_members "
                    "WHERE delegation_id = ?",
                    (str(row["delegation_id"]),),
                ).fetchone()
                if member_row is None:
                    raise RuntimeError(
                        "delegated thread creation record 的 collaboration "
                        f"member 缺失（fail closed）: key={idempotency_key!r}, "
                        f"delegation_id={row['delegation_id']!r}"
                    )
                if str(member_row["state"]) != "registering":
                    raise RuntimeError(
                        "collaboration member 非 registering，拒绝 publish "
                        f"转正（fail closed）: key={idempotency_key!r}, "
                        f"state={member_row['state']!r}"
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
            # owner binding 字段槽随发布原子建立（2.1）：locator 冻结、
            # 初始 prefix epoch=1（reason=initial）；后续 epoch/ToolSet/
            # activation 等 owner 事实由各 domain owner 经 typed 更新
            # 方法推进，本表不构成第二 writer。
            self._insert_thread_owner_binding_row(
                connection,
                thread_id=child_thread_id,
                final_relative_locator=final_relative_locator,
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
            )
            if row["collaboration_precondition_revision"] is not None:
                # ledger 与 thread catalog 原子可见性：member 在同一事务内
                # registering → published 并回填 child_thread_id。
                connection.execute(
                    "UPDATE collaboration_members "
                    "SET state = 'published', child_thread_id = ?, "
                    "updated_at = ? WHERE delegation_id = ?",
                    (
                        child_thread_id,
                        datetime.now(UTC).isoformat(),
                        str(row["delegation_id"]),
                    ),
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
        validate_thread_creation_key(idempotency_key)
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
            if (
                row["delegation_id"] is not None
                and row["collaboration_precondition_revision"] is not None
            ):
                # 同 delegation 不换绑：abort 在同一事务内定点取消 member，
                # 重试必须换新 delegation（register 对 cancelled fail closed）。
                connection.execute(
                    "UPDATE collaboration_members "
                    "SET state = 'cancelled', updated_at = ? "
                    "WHERE delegation_id = ? AND state = 'registering'",
                    (datetime.now(UTC).isoformat(), str(row["delegation_id"])),
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
        validate_thread_creation_key(idempotency_key)
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
    # 初始 execution admission intent（8.5-A 落库 + 8.5-B 消费状态机）
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
            execution_binding_id=str(row["execution_binding_id"]),
            job_id=str(row["job_id"]),
            binding_preimage_hash=str(row["binding_preimage_hash"]),
            claim_owner=(
                str(row["claim_owner"])
                if row["claim_owner"] is not None
                else None
            ),
            claim_generation=(
                int(row["claim_generation"])
                if row["claim_generation"] is not None
                else None
            ),
            last_error=(
                str(row["last_error"]) if row["last_error"] is not None else None
            ),
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
        接口契约，8.5-B 消费状态机的写入入口）。

        前置校验（fail closed）：对应 thread creation record 必须已
        ``published`` 且 child ID/initial_state 与入参一致；thread_catalog
        child row 必须可见（唯一可见性提交点已过）。同 admission key 幂等
        返回既有 intent（thread_id/initial_state/creation key 一致）；
        不一致冲突拒绝；同 thread 不同 admission key 由
        ``idx_thread_execution_intent_thread`` 唯一索引拒绝（崩溃不能留下
        重复初始 Job 的持久侧入口）。写入 ``state='pending'`` 并冻结软件
        生成的稳定 ``execution_binding_id``/``job_id``（由 admission
        key 确定性派生、重试不变）与完整 preimage hash；claim 字段为
        NULL，等待 8.5-B worker 领取。
        """
        validate_thread_creation_key(admission_idempotency_key)
        validate_thread_creation_key(creation_idempotency_key)
        validate_session_id(session_id)
        validate_thread_id(thread_id)
        if initial_state not in _INITIAL_STATE_VALUES:
            raise ValueError(f"initial_state 非法: {initial_state!r}")
        binding_id, job_id = derive_initial_execution_identity(
            admission_idempotency_key
        )
        binding_preimage_hash = compute_initial_execution_binding_preimage_hash(
            admission_idempotency_key=admission_idempotency_key,
            session_id=session_id,
            thread_id=thread_id,
            creation_idempotency_key=creation_idempotency_key,
            initial_state=initial_state,
            execution_binding_id=binding_id,
            job_id=job_id,
        )
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
            existing = _fetch_execution_intent_row(connection, admission_idempotency_key)
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
                if (
                    str(existing["execution_binding_id"]) != binding_id
                    or str(existing["job_id"]) != job_id
                    or str(existing["binding_preimage_hash"])
                    != binding_preimage_hash
                ):
                    raise RuntimeError(
                        "initial execution intent 冻结 identity 与确定性派生"
                        "值不一致（库被外部改动，fail closed）: "
                        f"admission_key={admission_idempotency_key!r}, "
                        f"existing_binding={existing['execution_binding_id']!r}"
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
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    admission_idempotency_key,
                    session_id,
                    thread_id,
                    creation_idempotency_key,
                    initial_state,
                    binding_id,
                    job_id,
                    binding_preimage_hash,
                    now_text,
                    now_text,
                ),
            )
            inserted = _fetch_execution_intent_row(connection, admission_idempotency_key)
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
        validate_thread_creation_key(admission_idempotency_key)
        self._ensure_open()
        row = _fetch_execution_intent_row(self._connection, admission_idempotency_key)
        if row is None:
            raise KeyError(
                "initial execution intent 不存在: "
                f"admission_key={admission_idempotency_key!r}"
            )
        return self._thread_execution_intent_from_row(row)

    def list_pending_initial_execution_intents(
        self,
    ) -> tuple[ThreadExecutionIntent, ...]:
        """只读返回全部 ``pending`` intent（8.5-B worker 状态索引）。

        纯 SQLite 状态索引查询（``idx_thread_execution_intent_state``），
        不扫目录、不读 ``thread.json``、不感知 Agent 配置或 task seed；
        结果按 ``(intent_created_at, admission_idempotency_key)`` 排序，
        消费顺序确定性。
        """
        self._ensure_open()
        rows = self._connection.execute(
            f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
            "FROM thread_execution_intents "
            "WHERE state = 'pending' "
            "ORDER BY intent_created_at, admission_idempotency_key"
        ).fetchall()
        return tuple(
            self._thread_execution_intent_from_row(row) for row in rows
        )

    def list_initial_execution_intents(
        self,
    ) -> tuple[ThreadExecutionIntent, ...]:
        """只读返回全部 intent（API 投影用；确定性排序）。"""
        self._ensure_open()
        rows = self._connection.execute(
            f"SELECT {_THREAD_EXECUTION_INTENT_COLUMNS} "
            "FROM thread_execution_intents "
            "ORDER BY intent_created_at, admission_idempotency_key"
        ).fetchall()
        return tuple(
            self._thread_execution_intent_from_row(row) for row in rows
        )

    def claim_initial_execution_intent(
        self,
        admission_idempotency_key: str,
        *,
        claim_owner: str,
        claim_generation: int,
    ) -> ThreadExecutionIntent:
        """领取初始 execution intent（8.5-B worker 并发闸门）。

        - intent 未领取（claim 字段 NULL）→ 写入 ``(claim_owner,
          claim_generation)`` 并返回；
        - 相同 ``(claim_owner, claim_generation)`` 重入 → 幂等返回
          （崩溃恢复重入同一 claim 的契约面）；
        - 同 owner 携带更高 generation → CAS 推进（恢复 owner 在验证旧
          holder 失效后接管；generation 只增不减）；
        - 同 owner 更低 generation、或不同 owner → ``RuntimeError``
          （不同 claim 冲突 fail closed；同一 admission 只允许一个有效
          claim）；
        - intent 非 ``pending`` → ``RuntimeError``（bound 后不可再
          领取）；不存在 → ``KeyError``。

        claim 不按 TTL 自动到期；intent 的可恢复事实一直保留。
        """
        validate_thread_creation_key(admission_idempotency_key)
        validate_claim_fields(claim_owner, claim_generation)
        with self._write_transaction() as connection:
            row = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if row is None:
                raise KeyError(
                    "initial execution intent 不存在，无法领取: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            state = str(row["state"])
            if state != "pending":
                raise RuntimeError(
                    "initial execution intent 非 pending，拒绝领取（bound "
                    f"后不可再领取）: admission_key="
                    f"{admission_idempotency_key!r}, state={state!r}"
                )
            existing_owner = row["claim_owner"]
            existing_generation = row["claim_generation"]
            if existing_owner is None:
                connection.execute(
                    "UPDATE thread_execution_intents "
                    "SET claim_owner = ?, claim_generation = ?, "
                    "intent_updated_at = ? "
                    "WHERE admission_idempotency_key = ?",
                    (
                        claim_owner,
                        claim_generation,
                        datetime.now(UTC).isoformat(),
                        admission_idempotency_key,
                    ),
                )
            elif str(existing_owner) == claim_owner:
                held_generation = int(existing_generation)
                if held_generation == claim_generation:
                    # 相同 claim 幂等：不更新任何字段。
                    pass
                elif claim_generation > held_generation:
                    # 恢复 owner 接管：CAS 推进 generation（只增不减）。
                    cursor = connection.execute(
                        "UPDATE thread_execution_intents "
                        "SET claim_generation = ?, intent_updated_at = ? "
                        "WHERE admission_idempotency_key = ? "
                        "AND claim_owner = ? AND claim_generation = ?",
                        (
                            claim_generation,
                            datetime.now(UTC).isoformat(),
                            admission_idempotency_key,
                            claim_owner,
                            held_generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "claim generation CAS 推进失败（并发改动，fail "
                            f"closed）: admission_key="
                            f"{admission_idempotency_key!r}"
                        )
                else:
                    raise RuntimeError(
                        "claim generation 过期（不得低于当前持有 "
                        f"generation，fail closed）: admission_key="
                        f"{admission_idempotency_key!r}, "
                        f"held={held_generation}, requested={claim_generation}"
                    )
            else:
                raise RuntimeError(
                    "initial execution intent 已被其他 claim 持有（不同 "
                    f"claim 冲突，fail closed）: admission_key="
                    f"{admission_idempotency_key!r}, "
                    f"held_owner={existing_owner!r}, "
                    f"requested_owner={claim_owner!r}"
                )
            updated = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内已确认存在。
                raise RuntimeError(
                    "initial execution intent claim 后不可见（事务异常）: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            return self._thread_execution_intent_from_row(updated)

    def mark_initial_execution_bound(
        self,
        admission_idempotency_key: str,
        *,
        execution_binding_id: str,
        job_id: str,
        claim_owner: str,
        claim_generation: int,
    ) -> ThreadExecutionIntent:
        """CAS ``pending -> bound``（8.5-B 绑定提交点）。

        - 校验稳定 identity 形态、claim 与当前持有 claim 一致；
        - 校验提交的 binding/job identity 与冻结值完全一致，并用落库时
          同一口径复算完整 preimage hash（任何 identity 漂移明确报错，
          不重基）；
        - CAS ``pending -> bound``（``WHERE state='pending'``）；
        - 已 ``bound`` 且提交完全一致 → 幂等返回（重复相同提交）；
          任何 identity/claim 漂移 → ``RuntimeError``；
        - intent 未领取或 claim 不符 → ``RuntimeError``；不存在 →
          ``KeyError``。
        """
        validate_thread_creation_key(admission_idempotency_key)
        _validate_execution_identity(execution_binding_id, job_id)
        validate_claim_fields(claim_owner, claim_generation)
        with self._write_transaction() as connection:
            row = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if row is None:
                raise KeyError(
                    "initial execution intent 不存在，无法标记 bound: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            state = str(row["state"])
            if row["claim_owner"] is None:
                raise RuntimeError(
                    "initial execution intent 未被领取，拒绝 mark bound: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            if (
                str(row["claim_owner"]) != claim_owner
                or int(row["claim_generation"]) != claim_generation
            ):
                raise RuntimeError(
                    "mark bound 的 claim 与当前持有 claim 不一致（fail "
                    f"closed）: admission_key="
                    f"{admission_idempotency_key!r}, "
                    f"held=({row['claim_owner']!r}, "
                    f"{row['claim_generation']!r}), "
                    f"requested=({claim_owner!r}, {claim_generation!r})"
                )
            if state == "bound":
                if (
                    str(row["execution_binding_id"]) != execution_binding_id
                    or str(row["job_id"]) != job_id
                ):
                    raise RuntimeError(
                        "initial execution intent 已 bound 且提交 identity "
                        f"漂移（fail closed）: admission_key="
                        f"{admission_idempotency_key!r}, "
                        f"frozen_binding={row['execution_binding_id']!r}, "
                        f"submitted_binding={execution_binding_id!r}"
                    )
                # 重复相同提交幂等：不改任何字段。
                return self._thread_execution_intent_from_row(row)
            if (
                str(row["execution_binding_id"]) != execution_binding_id
                or str(row["job_id"]) != job_id
            ):
                raise RuntimeError(
                    "mark bound 提交 identity 与冻结值漂移（fail closed，"
                    f"不重基）: admission_key="
                    f"{admission_idempotency_key!r}, "
                    f"frozen_binding={row['execution_binding_id']!r}, "
                    f"submitted_binding={execution_binding_id!r}, "
                    f"frozen_job={row['job_id']!r}, "
                    f"submitted_job={job_id!r}"
                )
            expected_hash = compute_initial_execution_binding_preimage_hash(
                admission_idempotency_key=str(
                    row["admission_idempotency_key"]
                ),
                session_id=str(row["session_id"]),
                thread_id=str(row["thread_id"]),
                creation_idempotency_key=str(
                    row["creation_idempotency_key"]
                ),
                initial_state=str(row["initial_state"]),
                execution_binding_id=execution_binding_id,
                job_id=job_id,
            )
            if expected_hash != str(row["binding_preimage_hash"]):
                raise RuntimeError(
                    "mark bound preimage 复算失败（binding_preimage_hash 与"
                    "冻结 identity 不一致，库被外部改动，fail closed）: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            cursor = connection.execute(
                "UPDATE thread_execution_intents "
                "SET state = 'bound', last_error = NULL, "
                "intent_updated_at = ? "
                "WHERE admission_idempotency_key = ? AND state = 'pending'",
                (
                    datetime.now(UTC).isoformat(),
                    admission_idempotency_key,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "mark bound CAS 失败（intent 已被并发推进，fail "
                    f"closed）: admission_key={admission_idempotency_key!r}"
                )
            updated = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内已确认存在。
                raise RuntimeError(
                    "initial execution intent mark bound 后不可见（事务异"
                    f"常）: admission_key={admission_idempotency_key!r}"
                )
            return self._thread_execution_intent_from_row(updated)

    def record_initial_execution_failure(
        self,
        admission_idempotency_key: str,
        *,
        claim_owner: str,
        claim_generation: int,
        last_error: str,
    ) -> ThreadExecutionIntent:
        """记录明确错误并保留 ``pending`` 可恢复事实（8.5-B）。

        只写 ``last_error``（非空字符串）与更新时间；不推进 state、
        不宣称 bound、不改 claim——intent 保持 ``pending`` 且 claim
        仍归当前持有者，可由同一 claim 幂等重入恢复。intent 非
        ``pending``、claim 不符或 ``last_error`` 为空 → fail
        closed；不存在 → ``KeyError``。
        """
        validate_thread_creation_key(admission_idempotency_key)
        validate_claim_fields(claim_owner, claim_generation)
        if not isinstance(last_error, str) or not last_error:
            raise ValueError(f"last_error 不能为空: {last_error!r}")
        with self._write_transaction() as connection:
            row = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if row is None:
                raise KeyError(
                    "initial execution intent 不存在，无法记录失败: "
                    f"admission_key={admission_idempotency_key!r}"
                )
            state = str(row["state"])
            if state != "pending":
                raise RuntimeError(
                    "initial execution intent 非 pending，无失败可记录（"
                    f"fail closed）: admission_key="
                    f"{admission_idempotency_key!r}, state={state!r}"
                )
            if (
                row["claim_owner"] is None
                or str(row["claim_owner"]) != claim_owner
                or int(row["claim_generation"]) != claim_generation
            ):
                raise RuntimeError(
                    "record failure 的 claim 与当前持有 claim 不一致（fail "
                    f"closed）: admission_key="
                    f"{admission_idempotency_key!r}"
                )
            cursor = connection.execute(
                "UPDATE thread_execution_intents "
                "SET last_error = ?, intent_updated_at = ? "
                "WHERE admission_idempotency_key = ? AND state = 'pending' "
                "AND claim_owner = ? AND claim_generation = ?",
                (
                    last_error,
                    datetime.now(UTC).isoformat(),
                    admission_idempotency_key,
                    claim_owner,
                    claim_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "record failure CAS 失败（intent 已被并发推进，fail "
                    f"closed）: admission_key={admission_idempotency_key!r}"
                )
            updated = _fetch_execution_intent_row(connection, admission_idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内已确认存在。
                raise RuntimeError(
                    "initial execution intent record failure 后不可见（事务"
                    f"异常）: admission_key={admission_idempotency_key!r}"
                )
            return self._thread_execution_intent_from_row(updated)
