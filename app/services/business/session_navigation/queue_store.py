"""会话目录异步 mutation 队列的持久层（OpenSpec 8.1-G/8.1-H）。

与 canonical ``nodes`` 表**同库**（workspace ``navigation/session-catalog.sqlite``）
的两张旁挂表承担 durable operation record 与导航事件 outbox：

- ``navigation_mutation_records``：typed 导航 operation 的 enqueue 事实、
  单调 ``queue_seq``、依赖、Folder ID 预留、terminal 状态与 compact tombstone。
  ``(gateway_id, workspace_id, actor, operation_id)`` 唯一；terminal 后行保留，
  用于阻止迟到重放的重复执行。
- ``navigation_events``：独立 ``navigation`` channel 的终态事件 outbox，
  ``(workspace_id, event_seq)`` 唯一且单调。事件与 operation terminal 状态在
  **同一个** SQLite 写事务提交（见 ``executor.py``）。

这两张表是纯加法式旁挂：不注册进 ``SessionCatalogStore`` 的
``_REQUIRED_TABLES_BY_VERSION``、不改 ``user_version``、不触碰 ``nodes`` /
creation / subtree 权威表，因此与 8.1-F 的启动校验不冲突。

catalog revision 直接采用 ``SessionCatalogStore.current_generation()``：它由
catalog 的每个已提交写事务推进一次，天然覆盖创建/删除/子树等所有目录变更，
因此客户端 pin 的 revision 不会漏掉本模块之外提交的目录变化。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import SessionCatalogStore
from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    NavigationMutationIntentDTO,
    validate_operation_id,
)

__all__ = [
    "NAVIGATION_BACKPRESSURE_PENDING_LIMIT",
    "NavigationBackpressureError",
    "NavigationEventRecord",
    "NavigationMutationConflictError",
    "NavigationMutationQueueStore",
    "NavigationMutationRecord",
    "compute_intent_preimage_hash",
]

# 单 workspace 未终态 operation 上限：超过即返回明确可重试的背压，绝不静默丢命令。
NAVIGATION_BACKPRESSURE_PENDING_LIMIT = 1000

_MUTATION_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS navigation_mutation_records (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    client_sequence INTEGER NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'committed', 'rejected', 'cancelled',
                  'dependency_failed')
    ),
    params_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    created_by_operation_id TEXT,
    target_node_id TEXT,
    reserved_node_id TEXT,
    result_node_id TEXT,
    result_node_revision INTEGER,
    committed_catalog_revision INTEGER,
    error_code TEXT,
    error_detail TEXT,
    pending_settlement INTEGER NOT NULL DEFAULT 0,
    holder_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    receipt_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, actor, operation_id)
)
"""

_MUTATION_RECORDS_QUEUE_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_mutation_queue "
    "ON navigation_mutation_records(workspace_id, queue_seq)"
)
_MUTATION_RECORDS_PENDING_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_navigation_mutation_pending "
    "ON navigation_mutation_records(workspace_id, state, queue_seq)"
)

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS navigation_events (
    workspace_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    result_state TEXT NOT NULL,
    committed_catalog_revision INTEGER,
    affected_node_ids_json TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, event_seq)
)
"""

_RECORD_COLUMNS = (
    "gateway_id, workspace_id, actor, operation_id, client_sequence, queue_seq, "
    "kind, state, params_json, preimage_hash, depends_on_json, "
    "created_by_operation_id, target_node_id, reserved_node_id, result_node_id, "
    "result_node_revision, committed_catalog_revision, error_code, error_detail, "
    "pending_settlement, "
    "holder_id, fencing_token, receipt_revision, created_at, updated_at"
)

_EVENT_COLUMNS = (
    "workspace_id, event_seq, operation_id, gateway_id, actor, queue_seq, kind, "
    "result_state, committed_catalog_revision, affected_node_ids_json, "
    "error_code, error_detail, created_at"
)


class NavigationBackpressureError(RuntimeError):
    """workspace 未终态 operation 超限：明确可重试，客户端须保留 outbox。"""


class NavigationMutationConflictError(RuntimeError):
    """同 ``(gateway, workspace, actor, operation_id)`` 但 preimage 不同。"""


@dataclass(frozen=True, slots=True)
class NavigationMutationRecord:
    """``navigation_mutation_records`` 行的不可变投影。"""

    gateway_id: str
    workspace_id: str
    actor: str
    operation_id: str
    client_sequence: int
    queue_seq: int
    kind: str
    state: str
    params: dict[str, object]
    preimage_hash: str
    depends_on: tuple[str, ...]
    created_by_operation_id: str | None
    target_node_id: str | None
    reserved_node_id: str | None
    result_node_id: str | None
    result_node_revision: int | None
    committed_catalog_revision: int | None
    error_code: str | None
    error_detail: str | None
    pending_settlement: bool
    holder_id: str | None
    fencing_token: int
    receipt_revision: int
    created_at: str
    updated_at: str

    @property
    def is_terminal(self) -> bool:
        return self.state in NAVIGATION_MUTATION_TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class NavigationEventRecord:
    """``navigation_events`` 行的不可变投影。"""

    workspace_id: str
    event_seq: int
    operation_id: str
    gateway_id: str
    actor: str
    queue_seq: int
    kind: str
    result_state: str
    committed_catalog_revision: int | None
    affected_node_ids: tuple[str, ...]
    error_code: str | None
    error_detail: str | None
    created_at: str


def compute_intent_preimage_hash(intent: NavigationMutationIntentDTO) -> str:
    """规范化 intent 参数并计算 preimage hash（同 key 异 preimage 冲突的判据）。

    只纳入会影响业务结果的规范化字段；``client_operation_id`` 本身与
    ``client_sequence``、``base_catalog_revision`` 不参与 preimage（重试时
    客户端可能给出新的快照修订，但意图未变）。
    """
    payload = {
        "kind": intent.kind,
        "target_node_id": intent.target_node_id,
        "created_by_operation_id": intent.created_by_operation_id,
        "name": intent.name,
        "parent_node_id": intent.parent_node_id,
        "recursive": intent.recursive,
        "depends_on": sorted(intent.depends_on),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _params_json(intent: NavigationMutationIntentDTO) -> str:
    return json.dumps(
        {
            "expected_revision": intent.expected_revision,
            "base_catalog_revision": intent.base_catalog_revision,
            "target_node_id": intent.target_node_id,
            "created_by_operation_id": intent.created_by_operation_id,
            "name": intent.name,
            "parent_node_id": intent.parent_node_id,
            "recursive": intent.recursive,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def read_catalog_revision(
    connection: sqlite3.Connection,
    *,
    committed: bool,
) -> int:
    """读取 catalog revision（= ``catalog_metadata`` 的单调 generation）。

    ``committed=False`` 用于只读快照口径（已提交事务的当前值）；``committed=True``
    用于写事务内的「本次提交后」口径（``write_transaction`` 在提交前自增一次
    generation，因此提交后值等于事务内读到的值 + 1）。两条口径共用本实现，避免
    客户端 pin 的 revision 与本模块上报的 revision 出现两套算法。
    """
    row = connection.execute(
        "SELECT generation FROM catalog_metadata WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "session catalog 缺少 catalog_metadata 单例行，无法确定 revision"
        )
    return int(row[0]) + 1 if committed else int(row[0])


class NavigationMutationQueueStore:
    """``navigation_mutation_records`` / ``navigation_events`` 的持久层。

    所有写方法都接受调用方的 ``sqlite3.Connection``（来自共享
    ``SessionCatalogStore.write_transaction()``），由调用方决定提交边界，
    从而把 node 变更、terminal record 与事件 outbox 放进同一个事务。
    """

    def __init__(self, store: SessionCatalogStore) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        self._store = store
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """幂等建旁挂表；已存在时不写事务（避免无谓推进 generation）。"""
        with self._store.read_transaction() as connection:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        if {"navigation_mutation_records", "navigation_events"} <= present:
            return
        with self._store.write_transaction() as connection:
            connection.execute(_MUTATION_RECORDS_DDL)
            connection.execute(_MUTATION_RECORDS_QUEUE_DDL)
            connection.execute(_MUTATION_RECORDS_PENDING_DDL)
            connection.execute(_EVENTS_DDL)

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------

    def enqueue_batch(
        self,
        connection: sqlite3.Connection,
        *,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        intents: list[NavigationMutationIntentDTO],
        now: str,
    ) -> list[NavigationMutationRecord]:
        """在调用方写事务内原子接受一批 intent，返回按 ``client_sequence`` 的 receipt。

        - 逐 intent 校验幂等：同 key 同 preimage → 复用既有行（不重复分配
          ``queue_seq``）；同 key 异 preimage → 冲突；
        - 顺序分配单调 ``queue_seq``，并为 ``create_folder`` 预留 canonical
          Folder ID（同批返回映射，不把临时 ID 写入 catalog）；
        - 依赖解析：同批依赖引用本批 ``client_operation_id``，跨批依赖引用
          已存在的 ``created_by_operation_id``；任一前置未终态则批次视为
          合法（执行期收敛），引用不存在的 operation 则冲突。
        """
        pending = self._count_pending(connection, workspace_id)
        if pending + len(intents) > NAVIGATION_BACKPRESSURE_PENDING_LIMIT:
            raise NavigationBackpressureError(
                "会话目录 operation 队列积压达到上限，请稍后重试并保留本地 outbox: "
                f"workspace_id={workspace_id}, pending={pending}, "
                f"batch={len(intents)}, limit={NAVIGATION_BACKPRESSURE_PENDING_LIMIT}"
            )
        batch_ids = {intent.client_operation_id for intent in intents}
        ordered = sorted(intents, key=lambda item: item.client_sequence)
        receipts: list[NavigationMutationRecord] = []
        for intent in ordered:
            preimage_hash = compute_intent_preimage_hash(intent)
            existing = self._fetch_record(
                connection, gateway_id, workspace_id, actor, intent.client_operation_id
            )
            if existing is not None:
                if existing["preimage_hash"] != preimage_hash:
                    raise NavigationMutationConflictError(
                        "同一 client_operation_id 携带不同 preimage，明确冲突: "
                        f"operation_id={intent.client_operation_id}"
                    )
                receipts.append(self._record_from_row(existing))
                continue
            self._require_dependencies_resolvable(
                connection,
                workspace_id=workspace_id,
                intent=intent,
                batch_ids=batch_ids,
            )
            queue_seq = self._next_queue_seq(connection, workspace_id)
            reserved_node_id = (
                create_prefixed_id("ses")
                if intent.kind == "create_folder"
                else None
            )
            connection.execute(
                "INSERT INTO navigation_mutation_records ("
                "gateway_id, workspace_id, actor, operation_id, client_sequence, "
                "queue_seq, kind, state, params_json, preimage_hash, "
                "depends_on_json, created_by_operation_id, target_node_id, "
                "reserved_node_id, receipt_revision, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    gateway_id,
                    workspace_id,
                    actor,
                    intent.client_operation_id,
                    intent.client_sequence,
                    queue_seq,
                    intent.kind,
                    _params_json(intent),
                    preimage_hash,
                    json.dumps(sorted(intent.depends_on)),
                    intent.created_by_operation_id,
                    intent.target_node_id,
                    reserved_node_id,
                    now,
                    now,
                ),
            )
            receipts.append(
                self._record_from_row(
                    self._require_record(
                        connection,
                        gateway_id,
                        workspace_id,
                        actor,
                        intent.client_operation_id,
                    )
                )
            )
        return receipts

    @staticmethod
    def _count_pending(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COUNT(*) FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state IN ('queued', 'running')",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _next_queue_seq(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM navigation_mutation_records "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    def _require_dependencies_resolvable(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        intent: NavigationMutationIntentDTO,
        batch_ids: set[str],
    ) -> None:
        for dependency_id in intent.depends_on:
            if dependency_id in batch_ids:
                continue
            self._require_referenced_operation(
                connection, workspace_id, dependency_id, intent
            )
        if intent.created_by_operation_id is not None:
            if intent.created_by_operation_id == intent.client_operation_id:
                raise ValueError("intent 不能引用自身作为 created_by_operation_id")
            if intent.created_by_operation_id not in batch_ids:
                self._require_referenced_operation(
                    connection,
                    workspace_id,
                    intent.created_by_operation_id,
                    intent,
                )

    def _require_referenced_operation(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation_id: str,
        intent: NavigationMutationIntentDTO,
    ) -> None:
        row = connection.execute(
            "SELECT queue_seq FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
            (workspace_id, operation_id),
        ).fetchone()
        if row is None:
            raise NavigationMutationConflictError(
                "跨批依赖引用的 operation 尚未被 durable 接受: "
                f"intent={intent.client_operation_id}, dependency={operation_id}"
            )

    # ------------------------------------------------------------------
    # 单条读写
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_record(
        connection: sqlite3.Connection,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        operation_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
            "AND operation_id = ?",
            (gateway_id, workspace_id, actor, operation_id),
        ).fetchone()

    def _require_record(
        self,
        connection: sqlite3.Connection,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        operation_id: str,
    ) -> sqlite3.Row:
        row = self._fetch_record(connection, gateway_id, workspace_id, actor, operation_id)
        if row is None:
            raise KeyError(f"会话目录 operation 不存在: {operation_id}")
        return row

    def get_record(
        self,
        *,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        operation_id: str,
    ) -> NavigationMutationRecord | None:
        """按精确 ID 在只读单事务内查询 durable record；缺失返回 None。"""
        validate_operation_id(operation_id)
        with self._store.read_transaction() as connection:
            row = self._fetch_record(
                connection, gateway_id, workspace_id, actor, operation_id
            )
            return None if row is None else self._record_from_row(row)

    def fetch_record_in(
        self,
        connection: sqlite3.Connection,
        *,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        operation_id: str,
    ) -> NavigationMutationRecord | None:
        """在调用方已开事务的连接上查同一行；缺失返回 None。

        供 executor 在写事务内重读（不得再开嵌套事务）。
        """
        row = self._fetch_record(
            connection, gateway_id, workspace_id, actor, operation_id
        )
        return None if row is None else self._record_from_row(row)

    def list_records(
        self,
        *,
        gateway_id: str,
        workspace_id: str,
        actor: str,
        operation_ids: list[str],
    ) -> tuple[list[NavigationMutationRecord], list[str]]:
        """按精确 ID 批量查询：返回 (命中记录, 未知 ID 列表)。

        未知 ID 显式回报（而不是当作失败）：客户端据此保留 pending 并原 ID
        重试，不因一次查询落空就回退本地状态。
        """
        for operation_id in operation_ids:
            validate_operation_id(operation_id)
        found: list[NavigationMutationRecord] = []
        unknown: list[str] = []
        with self._store.read_transaction() as connection:
            for operation_id in operation_ids:
                row = self._fetch_record(
                    connection, gateway_id, workspace_id, actor, operation_id
                )
                if row is None:
                    unknown.append(operation_id)
                else:
                    found.append(self._record_from_row(row))
        return found, unknown

    def next_runnable(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
    ) -> NavigationMutationRecord | None:
        """按 ``queue_seq`` FIFO 返回下一条可执行 operation（不跳过较早未终态）。

        ``queued`` 且全部前置依赖已 terminal 的 operation 才可运行；前置失败
        的后继由 :meth:`mark_dependency_failed_successors` 直接终结，不会出现
        「前置未终态却先跑后继」的乱序。
        """
        rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state = 'queued' ORDER BY queue_seq",
            (workspace_id,),
        ).fetchall()
        records = [self._record_from_row(row) for row in rows]
        for record in records:
            if self._dependencies_terminal(connection, workspace_id, record):
                return record
        return None

    def _dependencies_terminal(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        record: NavigationMutationRecord,
    ) -> bool:
        for dependency_id in record.depends_on:
            row = connection.execute(
                "SELECT state FROM navigation_mutation_records "
                "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
                (workspace_id, dependency_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "已 durable 接受的依赖 operation 缺失（catalog 被外部改动）: "
                    f"operation={record.operation_id}, dependency={dependency_id}"
                )
            if str(row[0]) not in NAVIGATION_MUTATION_TERMINAL_STATES:
                return False
        if record.created_by_operation_id is not None:
            row = connection.execute(
                "SELECT state FROM navigation_mutation_records "
                "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
                (workspace_id, record.created_by_operation_id),
            ).fetchone()
            if row is None or str(row[0]) not in NAVIGATION_MUTATION_TERMINAL_STATES:
                return False
        return True

    # ------------------------------------------------------------------
    # 状态转移（全部在调用方写事务内）
    # ------------------------------------------------------------------

    def claim_running(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        holder_id: str,
        now: str,
    ) -> NavigationMutationRecord | None:
        """严格 ``queued → running`` CAS 领取；已被他人领取时返回 None。

        ``WHERE state='queued'`` 守卫保证同一条 operation 只有一个 owner 能提交
        执行事务：并发 owner 中失败的一方返回 None 并跳过，绝不重复应用副作用。
        崩溃遗留的 ``running`` 行先由 :meth:`recover_in_flight` 重置为 ``queued``
        才能被重新领取——因此接管路径显式且可审计，不靠模糊的 token 递增。
        """
        cursor = connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = ?, fencing_token = fencing_token + 1, "
            "receipt_revision = receipt_revision + 1, updated_at = ? "
            "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
            "AND operation_id = ? AND state = 'queued'",
            (
                holder_id,
                now,
                record.gateway_id,
                record.workspace_id,
                record.actor,
                record.operation_id,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return self._record_from_row(
            self._require_record(
                connection,
                record.gateway_id,
                record.workspace_id,
                record.actor,
                record.operation_id,
            )
        )

    def recover_in_flight(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        now: str,
    ) -> int:
        """把崩溃前遗留的 ``running`` 行重置为 ``queued``，供新 owner 继续执行。

        安全性依据：普通 node mutation 的 node 变更、terminal record 与事件在
        **同一事务**提交，因此不存在「已改节点但无 terminal」的遗留；删除类
        operation 以 ``operation_id`` 为 idempotency key，重入即按原 record 定点
        继续。因此重置不产生重复副作用，也不重排已接受依赖（``queue_seq`` 不变）。

        返回被重置的 operation 数。
        """
        cursor = connection.execute(
            "UPDATE navigation_mutation_records SET state = 'queued', "
            "holder_id = NULL, receipt_revision = receipt_revision + 1, "
            "updated_at = ? WHERE workspace_id = ? AND state = 'running'",
            (now, workspace_id),
        )
        return int(cursor.rowcount)

    def finish_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        expected_fencing_token: int,
        state: str,
        now: str,
        result_node_id: str | None = None,
        result_node_revision: int | None = None,
        committed_catalog_revision: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        pending_settlement: bool = False,
    ) -> NavigationMutationRecord:
        """把 operation 推进到终态（或 ``dependency_failed``），保留 compact tombstone。

        terminal 行**不删除**：它是阻止迟到重放的唯一凭据，同 key 重试只会拿
        到原 terminal receipt。``expected_fencing_token`` 不匹配说明本 owner 已
        被接管，写入被 CAS 拒绝（旧 token 必须失败）。
        """
        if state not in NAVIGATION_MUTATION_TERMINAL_STATES:
            raise ValueError(f"finish_terminal 只接受终态状态: {state!r}")
        cursor = connection.execute(
            "UPDATE navigation_mutation_records SET state = ?, result_node_id = ?, "
            "result_node_revision = ?, committed_catalog_revision = ?, "
            "error_code = ?, error_detail = ?, "
            "pending_settlement = ?, receipt_revision = receipt_revision + 1, "
            "updated_at = ? "
            "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
            "AND operation_id = ? AND fencing_token = ?",
            (
                state,
                result_node_id,
                result_node_revision,
                committed_catalog_revision,
                error_code,
                error_detail,
                1 if pending_settlement else 0,
                now,
                record.gateway_id,
                record.workspace_id,
                record.actor,
                record.operation_id,
                expected_fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "operation fencing token 已失效，拒绝写入终态（已被新 owner 接管）: "
                f"operation_id={record.operation_id}, "
                f"expected_fencing_token={expected_fencing_token}"
            )
        return self._record_from_row(
            self._require_record(
                connection,
                record.gateway_id,
                record.workspace_id,
                record.actor,
                record.operation_id,
            )
        )

    def mark_dependency_failed_successors(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        failed_operation_id: str,
        now: str,
    ) -> list[NavigationMutationRecord]:
        """把 ``failed_operation_id`` 的传递后继从 ``queued`` 直接终结为
        ``dependency_failed``（无任何业务副作用）。

        只处理仍为 ``queued`` 的行：已 ``running``/终态的行不动（不重排、不
        伪造结果）。返回被终结的记录，供同事务写事件 outbox。
        """
        queued_rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state = 'queued' ORDER BY queue_seq",
            (workspace_id,),
        ).fetchall()
        queued = {row["operation_id"]: self._record_from_row(row) for row in queued_rows}
        failed: list[NavigationMutationRecord] = []
        poisoned = {failed_operation_id}
        changed = True
        while changed:
            changed = False
            for operation_id, record in queued.items():
                if operation_id in poisoned:
                    continue
                references = set(record.depends_on)
                if record.created_by_operation_id is not None:
                    references.add(record.created_by_operation_id)
                if references & poisoned:
                    poisoned.add(operation_id)
                    failed.append(record)
                    changed = True
        for record in failed:
            connection.execute(
                "UPDATE navigation_mutation_records SET state = 'dependency_failed', "
                "error_code = 'dependency_failed', error_detail = ?, "
                "receipt_revision = receipt_revision + 1, updated_at = ? "
                "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
                "AND operation_id = ? AND state = 'queued'",
                (
                    f"前置 operation 未成功: {failed_operation_id}",
                    now,
                    record.gateway_id,
                    record.workspace_id,
                    record.actor,
                    record.operation_id,
                ),
            )
        return failed

    # ------------------------------------------------------------------
    # 事件 outbox
    # ------------------------------------------------------------------

    @staticmethod
    def _next_event_seq(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM navigation_events "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        affected_node_ids: list[str],
        now: str,
    ) -> NavigationEventRecord:
        """在**同一** catalog 写事务内追加终态事件（与 operation terminal 同事务）。"""
        if not record.is_terminal:
            raise RuntimeError(
                f"只允许为终态 operation 追加导航事件: {record.operation_id}"
            )
        event_seq = self._next_event_seq(connection, record.workspace_id)
        connection.execute(
            "INSERT INTO navigation_events ("
            "workspace_id, event_seq, operation_id, gateway_id, actor, queue_seq, "
            "kind, result_state, committed_catalog_revision, "
            "affected_node_ids_json, error_code, error_detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.workspace_id,
                event_seq,
                record.operation_id,
                record.gateway_id,
                record.actor,
                record.queue_seq,
                record.kind,
                record.state,
                record.committed_catalog_revision,
                json.dumps(sorted(set(affected_node_ids))),
                record.error_code,
                record.error_detail,
                now,
            ),
        )
        row = connection.execute(
            f"SELECT {_EVENT_COLUMNS} FROM navigation_events "
            "WHERE workspace_id = ? AND event_seq = ?",
            (record.workspace_id, event_seq),
        ).fetchone()
        return self._event_from_row(row)

    def list_events(
        self,
        *,
        workspace_id: str,
        after: int,
        limit: int,
    ) -> tuple[list[NavigationEventRecord], int]:
        """返回 ``(event_seq > after)`` 的前 ``limit`` 条事件与当前水位。

        cursor 由单调 ``event_seq`` 承载：重复的 ``after`` 会重复返回相同集合
        （客户端按 ``event_seq``/``operation_id`` 去重即可），不存在「跳过未读
        事件」的窗口。水位在同一次只读事务内取得。
        """
        with self._store.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM navigation_events "
                "WHERE workspace_id = ? AND event_seq > ? ORDER BY event_seq LIMIT ?",
                (workspace_id, after, limit),
            ).fetchall()
            watermark = self._event_watermark(connection, workspace_id)
        return [self._event_from_row(row) for row in rows], watermark

    def event_watermark_in(self, connection: sqlite3.Connection, workspace_id: str) -> int:
        """在调用方只读事务内取事件水位（供 snapshot 与 revision 同一快照）。"""
        return self._event_watermark(connection, workspace_id)

    @staticmethod
    def _event_watermark(connection: sqlite3.Connection, workspace_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM navigation_events "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # 行投影
    # ------------------------------------------------------------------

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> NavigationMutationRecord:
        return NavigationMutationRecord(
            gateway_id=str(row["gateway_id"]),
            workspace_id=str(row["workspace_id"]),
            actor=str(row["actor"]),
            operation_id=str(row["operation_id"]),
            client_sequence=int(row["client_sequence"]),
            queue_seq=int(row["queue_seq"]),
            kind=str(row["kind"]),
            state=str(row["state"]),
            params=json.loads(str(row["params_json"])),
            preimage_hash=str(row["preimage_hash"]),
            depends_on=tuple(json.loads(str(row["depends_on_json"]))),
            created_by_operation_id=row["created_by_operation_id"],
            target_node_id=row["target_node_id"],
            reserved_node_id=row["reserved_node_id"],
            result_node_id=row["result_node_id"],
            result_node_revision=row["result_node_revision"],
            committed_catalog_revision=row["committed_catalog_revision"],
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            pending_settlement=bool(row["pending_settlement"]),
            holder_id=row["holder_id"],
            fencing_token=int(row["fencing_token"]),
            receipt_revision=int(row["receipt_revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> NavigationEventRecord:
        return NavigationEventRecord(
            workspace_id=str(row["workspace_id"]),
            event_seq=int(row["event_seq"]),
            operation_id=str(row["operation_id"]),
            gateway_id=str(row["gateway_id"]),
            actor=str(row["actor"]),
            queue_seq=int(row["queue_seq"]),
            kind=str(row["kind"]),
            result_state=str(row["result_state"]),
            committed_catalog_revision=row["committed_catalog_revision"],
            affected_node_ids=tuple(json.loads(str(row["affected_node_ids_json"]))),
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            created_at=str(row["created_at"]),
        )
