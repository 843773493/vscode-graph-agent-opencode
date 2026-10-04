"""会话目录异步 mutation 队列的持久层门面（OpenSpec 8.1-G/8.1-H）。

与 canonical ``nodes`` 表**同库**（workspace ``navigation/session-catalog.sqlite``）
的两张旁挂表承担 durable operation record 与导航事件 outbox：

- ``navigation_mutation_records``：typed 导航 operation 的 enqueue 事实、
  单调 ``queue_seq``、依赖、Folder ID 预留、terminal 状态与 compact tombstone。
  ``(gateway_id, workspace_id, actor, operation_id)`` 唯一；terminal 后行保留，
  用于阻止迟到重放的重复执行。
- ``navigation_events``：独立 ``navigation`` channel 的终态事件 outbox，
  ``(workspace_id, event_seq)`` 唯一且单调。事件与 operation terminal 状态在
  **同一个** SQLite 写事务提交（见 ``executor.py``）。

本模块是门面：物理 schema 在 ``queue_schema.py``，「记录模型」在
``queue_records.py``，事件表链在 ``queue_events.py``，依赖解析与失败传播在
``queue_dependencies.py``；本模块保留队列语义方法并原路径重导出全部对外符号，
外部调用方的导入路径不随拆分改变。

catalog revision 直接采用 ``SessionCatalogStore.current_generation()``：它由
catalog 的每个已提交写事务推进一次，天然覆盖创建/删除/子树等所有目录变更，
因此客户端 pin 的 revision 不会漏掉本模块之外提交的目录变化。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import json
import sqlite3

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import SessionCatalogStore
from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    NavigationMutationIntentDTO,
    validate_operation_id,
)
from app.services.business.session_navigation.queue_dependencies import (
    NavigationDependencyMixin,
)
from app.services.business.session_navigation.queue_events import (
    NavigationEventOutboxMixin,
)
from app.services.business.session_navigation.queue_records import (
    _RECORD_COLUMNS,
    NAVIGATION_BACKPRESSURE_PENDING_LIMIT,
    NavigationBackpressureError,
    NavigationEventRecord,
    NavigationMutationConflictError,
    NavigationMutationRecord,
    NavigationQueueOwner,
    NavigationQueueOwnerError,
    _event_from_row,
    _params_json,
    _record_from_row,
    compute_intent_preimage_hash,
)
from app.services.business.session_navigation.queue_schema import (
    ensure_navigation_queue_tables,
)

__all__ = [
    "NAVIGATION_BACKPRESSURE_PENDING_LIMIT",
    "NavigationBackpressureError",
    "NavigationEventRecord",
    "NavigationMutationConflictError",
    "NavigationMutationQueueStore",
    "NavigationMutationRecord",
    "NavigationQueueOwner",
    "NavigationQueueOwnerError",
    "compute_intent_preimage_hash",
]


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


class NavigationMutationQueueStore(
    NavigationEventOutboxMixin,
    NavigationDependencyMixin,
):
    """``navigation_mutation_records`` / ``navigation_events`` 的持久层。

    所有写方法都接受调用方的 ``sqlite3.Connection``（来自共享
    ``SessionCatalogStore.write_transaction()``），由调用方决定提交边界，
    从而把 node 变更、terminal record 与事件 outbox 放进同一个事务。
    """

    #: 两个行投影的唯一构造点（模块级实现），固定为类静态方法以保持投影入口不变。
    _record_from_row = staticmethod(_record_from_row)
    _event_from_row = staticmethod(_event_from_row)

    def __init__(self, store: SessionCatalogStore) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        self._store = store
        ensure_navigation_queue_tables(self._store)

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
        if len(batch_ids) != len(ordered):
            raise ValueError("同批 client_operation_id 必须唯一")
        batch_positions = {
            intent.client_operation_id: position
            for position, intent in enumerate(ordered)
        }
        receipts: list[NavigationMutationRecord] = []
        for position, intent in enumerate(ordered):
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
            self._require_batch_dependencies_precede(
                connection,
                workspace_id=workspace_id,
                intent=intent,
                batch_positions=batch_positions,
                position=position,
            )
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

    @staticmethod
    def _require_batch_dependencies_precede(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        intent: NavigationMutationIntentDTO,
        batch_positions: dict[str, int],
        position: int,
    ) -> None:
        """拒绝尚未 durable 的同批后序依赖，避免严格 FIFO 队首永久停滞。"""
        dependency_ids = set(intent.depends_on)
        if intent.created_by_operation_id is not None:
            dependency_ids.add(intent.created_by_operation_id)
        for dependency_id in dependency_ids:
            if dependency_id == intent.client_operation_id:
                raise ValueError(
                    "intent 不能依赖自身: "
                    f"operation_id={intent.client_operation_id}"
                )
            dependency_position = batch_positions.get(dependency_id)
            if dependency_position is None or dependency_position < position:
                continue
            row = connection.execute(
                "SELECT 1 FROM navigation_mutation_records "
                "WHERE workspace_id = ? AND operation_id = ? LIMIT 1",
                (workspace_id, dependency_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "同批依赖必须先于依赖方入队，避免 FIFO 队首永久阻塞: "
                    f"operation_id={intent.client_operation_id}, "
                    f"dependency_id={dependency_id}"
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

    def find_delete_operation_in(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        operation_id: str,
    ) -> NavigationMutationRecord | None:
        """按 subtree delete 的稳定 key 找对应递归删除 operation。"""
        rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND operation_id = ? "
            "AND kind IN ('delete_folder', 'delete_session') LIMIT 2",
            (workspace_id, operation_id),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(
                "subtree delete key 对应多个导航 operation，拒绝猜测 scope: "
                f"workspace_id={workspace_id}, operation_id={operation_id}"
            )
        return None if not rows else self._record_from_row(rows[0])

    def settle_delete_operation_in(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        now: str,
    ) -> NavigationMutationRecord:
        """将已 committed 的递归删除 receipt 从 pending settlement 推进为 settled。"""
        cursor = connection.execute(
            "UPDATE navigation_mutation_records SET pending_settlement = 0, "
            "receipt_revision = receipt_revision + 1, updated_at = ? "
            "WHERE gateway_id = ? AND workspace_id = ? AND actor = ? "
            "AND operation_id = ? AND state = 'committed' "
            "AND pending_settlement = 1",
            (
                now,
                record.gateway_id,
                record.workspace_id,
                record.actor,
                record.operation_id,
            ),
        )
        current = self.fetch_record_in(
            connection,
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=record.operation_id,
        )
        if current is None:
            raise KeyError(f"会话目录 operation 不存在: {record.operation_id}")
        if cursor.rowcount == 1:
            return current
        if current.state == "committed" and not current.pending_settlement:
            return current
        raise RuntimeError(
            "递归删除 settlement CAS 失败，operation 不在 committed/pending 状态: "
            f"operation_id={record.operation_id}, state={current.state}, "
            f"pending_settlement={current.pending_settlement}"
        )

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
        row = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state IN ('queued', 'running') "
            "ORDER BY queue_seq LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._record_from_row(row)
        if record.state != "queued":
            return None
        return (
            record
            if self._dependencies_terminal(connection, workspace_id, record)
            else None
        )

    @staticmethod
    def acquire_owner(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        now: str,
    ) -> NavigationQueueOwner:
        """OS lock 已证明旧 owner 退出后，CAS 递增 durable generation。"""
        connection.execute(
            "INSERT INTO navigation_queue_owners "
            "(workspace_id, owner_id, owner_generation, updated_at) "
            "VALUES (?, ?, 1, ?) ON CONFLICT(workspace_id) DO UPDATE SET "
            "owner_id = excluded.owner_id, "
            "owner_generation = navigation_queue_owners.owner_generation + 1, "
            "updated_at = excluded.updated_at",
            (workspace_id, owner_id, now),
        )
        row = connection.execute(
            "SELECT owner_generation FROM navigation_queue_owners "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"queue owner generation 更新后缺失: workspace_id={workspace_id}"
            )
        return NavigationQueueOwner(
            workspace_id=workspace_id,
            owner_id=owner_id,
            generation=int(row[0]),
        )

    @staticmethod
    def require_owner_generation(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        generation: int,
    ) -> None:
        """在业务 claim 同一事务中校验本 worker 的 durable fencing token。"""
        row = connection.execute(
            "SELECT owner_id, owner_generation FROM navigation_queue_owners "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if (
            row is None
            or str(row["owner_id"]) != owner_id
            or int(row["owner_generation"]) != generation
        ):
            current = (
                None
                if row is None
                else f"{row['owner_id']}:{int(row['owner_generation'])}"
            )
            raise NavigationQueueOwnerError(
                "navigation queue owner fencing token 已失效: "
                f"workspace_id={workspace_id}, expected={owner_id}:{generation}, "
                f"current={current}"
            )

    # ------------------------------------------------------------------
    # 状态转移（全部在调用方写事务内）
    # ------------------------------------------------------------------

    def claim_running(
        self,
        connection: sqlite3.Connection,
        *,
        record: NavigationMutationRecord,
        holder_id: str,
        owner_id: str,
        owner_generation: int,
        now: str,
    ) -> NavigationMutationRecord | None:
        """严格 ``queued → running`` CAS 领取；已被他人领取时返回 None。

        ``WHERE state='queued'`` 守卫保证同一条 operation 只有一个 owner 能提交
        执行事务：并发 owner 中失败的一方返回 None 并跳过，绝不重复应用副作用。
        崩溃遗留的 ``running`` 行先由 :meth:`recover_in_flight` 重置为 ``queued``
        才能被重新领取——因此接管路径显式且可审计，不靠模糊的 token 递增。
        """
        self.require_owner_generation(
            connection,
            workspace_id=record.workspace_id,
            owner_id=owner_id,
            generation=owner_generation,
        )
        if holder_id != f"{owner_id}:{owner_generation}":
            raise NavigationQueueOwnerError(
                "operation holder 与 durable queue owner generation 不一致: "
                f"holder_id={holder_id}, owner_generation={owner_generation}"
            )
        head = connection.execute(
            "SELECT operation_id, queue_seq, state FROM navigation_mutation_records "
            "WHERE workspace_id = ? AND state IN ('queued', 'running') "
            "ORDER BY queue_seq LIMIT 1",
            (record.workspace_id,),
        ).fetchone()
        if (
            head is None
            or str(head["operation_id"]) != record.operation_id
            or int(head["queue_seq"]) != record.queue_seq
            or str(head["state"]) != "queued"
        ):
            return None
        current = self.fetch_record_in(
            connection,
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=record.operation_id,
        )
        if current is None or not self._dependencies_terminal(
            connection, record.workspace_id, current
        ):
            return None
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
        owner_id: str,
        owner_generation: int,
        now: str,
    ) -> int:
        """把崩溃前遗留的 ``running`` 行重置为 ``queued``，供新 owner 继续执行。

        安全性依据：普通 node mutation 的 node 变更、terminal record 与事件在
        **同一事务**提交，因此不存在「已改节点但无 terminal」的遗留；删除类
        operation 以 ``operation_id`` 为 idempotency key，重入即按原 record 定点
        继续。因此重置不产生重复副作用，也不重排已接受依赖（``queue_seq`` 不变）。

        返回被重置的 operation 数。
        """
        self.require_owner_generation(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            generation=owner_generation,
        )
        cursor = connection.execute(
            "UPDATE navigation_mutation_records SET state = 'queued', "
            "holder_id = NULL, receipt_revision = receipt_revision + 1, "
            "updated_at = ? WHERE workspace_id = ? AND state = 'running' "
            "AND holder_id != ?",
            (now, workspace_id, f"{owner_id}:{owner_generation}"),
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
