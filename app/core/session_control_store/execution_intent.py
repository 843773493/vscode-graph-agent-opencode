"""per-session ``session-control.sqlite`` 的初始 execution intent 垂直链路。

承载 ``thread_execution_intents`` 表的唯一实现：表 DDL 与索引、稳定 binding/
job identity 的确定性派生、binding preimage hash 计算、行投影
``ThreadExecutionIntent``，以及 create-or-get / claim / mark bound / record
failure 的消费状态机。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.core.session_control_primitives import (
    EXECUTION_BINDING_ID_PATTERN,
    EXECUTION_JOB_ID_PATTERN,
    validate_claim_fields,
    validate_thread_creation_key,
)
from app.core.session_control_store.sql import SELECT_THREAD_CATALOG_CHILD_ROW
from app.core.session_control_store.thread_creation_record import (
    _INITIAL_STATE_VALUES,
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


class ExecutionIntentMixin:
    """初始 execution admission intent 的消费状态机方法族。"""

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
                SELECT_THREAD_CATALOG_CHILD_ROW,
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
