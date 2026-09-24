"""会话目录导航 mutation 的**唯一**执行引擎（OpenSpec 8.1-G）。

本模块是目录写操作的单一实现：异步 enqueue worker 与保留的同步目录 API
都只是它的调用方，不存在第二套写入逻辑（对齐 AGENTS.md「彻底根除双轨」）。

执行纪律（design.md §9.0）：

1. 单 workspace 按 ``queue_seq`` FIFO 执行；前置失败的后继已由队列层直接终结
   为 ``dependency_failed``，不会乱序或重复应用。
2. 普通 operation 在 workspace catalog **单一 SQLite 写事务**内重新校验目标
   node revision / 父节点 active / 无环 / 同名兄弟，提交 node 变更、terminal
   record 与导航事件 outbox 为同一事务；拒绝路径只提交 terminal reason 与事件，
   node 变更随事务整体回滚。
3. 递归删除复用 ``SessionSubtreeDeleteService``（其 mark 事务即导航逻辑
   committed 点），随后单独提交 terminal record 与事件，并区分 logical committed
   与 physical settlement。
4. 领取（``queued → running`` + 递增 fencing token）与终局写入在同一事务内完成，
   因此不存在「已改节点但无 terminal」的窗口：并发 owner 只有一个事务能提交，
   另一个重读到终态后幂等 no-op；崩溃遗留的 ``running`` 行可被新 owner 复用
   fencing token 继续（幂等恢复，不重排 queue_seq）。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCatalogStore,
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
)
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
)
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.core.sqlite_state import utc_now_text
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationMutationRecord,
    read_catalog_revision,
)

__all__ = [
    "NavigationExecutionOutcome",
    "NavigationMutationExecutor",
    "NavigationSkipped",
]

# 删除执行器：以确定性 operation_id 进入**共享**子树删除流。生产装配注入保持
# JobService 删除 admission 与后台任务预检的版本；未注入时退回 resolver 的共享
# ``delete_subtree``（同一删除流，无第二套实现）。
DeleteRunner = Callable[[str, str], Awaitable[SubtreeDeleteResult]]


@dataclass(frozen=True, slots=True)
class NavigationExecutionOutcome:
    """一次 operation 执行的终局观测（worker 与同步 façade 共用）。

    ``settled`` 为假表示逻辑已 committed 但物理排空尚未完成（递归删除），
    调用方据此上报 ``pending_settlement``，不得假报全部完成。
    """

    record: NavigationMutationRecord
    settled: bool


class NavigationSkipped(Exception):
    """本 owner 未取得该 operation（已被其它 owner 领取）：本轮不执行任何变更。

    内部信号，不向操作方暴露：``drain``/``execute_next`` 捕获后继续尝试队列中
    的下一条可执行 operation。继承 ``Exception`` 而非 ``RuntimeError``：本模块
    用 ``RuntimeError`` 表示「业务语义冲突、应写成 rejected 终态」，领取失败不是
    业务拒绝，必须逃出该分类，否则会把「被他人抢先」误报成 failed operation。
    """

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(f"导航 operation 已被其它 owner 领取: {operation_id}")


class NavigationMutationExecutor:
    """导航 operation 的唯一执行引擎。"""

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        workspace_id: str,
        queue: NavigationMutationQueueStore,
        path_resolver: SessionCatalogPathResolver,
        gate: NavigationTopologyGate | None = None,
        delete_runner: DeleteRunner | None = None,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        self._store = store
        self._workspace_id = workspace_id
        self._queue = queue
        # 递归删除必须复用共享的子树删除流（含容器绑定的运行时 drain 回调），
        # 因此这里只经 resolver 进入，不另建 delete service 实例。
        self._path_resolver = path_resolver
        self._delete_runner = delete_runner
        self._gate = (
            gate if gate is not None else NavigationTopologyGate(store.sessions_root)
        )

    # ------------------------------------------------------------------
    # 队列驱动
    # ------------------------------------------------------------------

    async def execute_next(self) -> NavigationExecutionOutcome | None:
        """执行下一条 runnable operation；None 表示队列已无可执行项。

        被其它 owner 抢先领取的 operation 由 :class:`NavigationSkipped` 标识，
        此处继续尝试下一条（``next_runnable`` 只返回 ``queued``，已被领取的
        不会再次返回，因此循环必然前进）。
        """
        while True:
            with self._store.read_transaction() as connection:
                candidate = self._queue.next_runnable(connection, self._workspace_id)
            if candidate is None:
                return None
            try:
                return await self.execute_record(candidate)
            except NavigationSkipped:
                continue

    async def drain(self, *, max_operations: int = 1000) -> list[NavigationExecutionOutcome]:
        """按 ``queue_seq`` FIFO 连续执行直到无可执行项（有界）。"""
        outcomes: list[NavigationExecutionOutcome] = []
        for _ in range(max_operations):
            try:
                outcome = await self.execute_next()
            except NavigationSkipped:
                continue
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    async def recover_in_flight(self) -> int:
        """新 owner 启动时把崩溃遗留的 ``running`` 行重置为 ``queued``。"""
        with self._store.write_transaction() as connection:
            return self._queue.recover_in_flight(
                connection,
                workspace_id=self._workspace_id,
                now=utc_now_text(),
            )

    async def execute_record(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationExecutionOutcome:
        """执行单条已 durable 接受的 operation（终态为幂等 no-op）。

        未取得执行权时抛 :class:`NavigationSkipped`（不是假终态）：调用方据此
        区分「已完成」与「本轮被其它 owner 抢先」。
        """
        if record.is_terminal:
            return NavigationExecutionOutcome(record=record, settled=True)
        if record.kind in ("delete_folder", "delete_session"):
            return await self._execute_delete(record)
        return await self._execute_node_mutation(record)

    # ------------------------------------------------------------------
    # 普通 node mutation（create/rename/move）：单事务提交
    # ------------------------------------------------------------------

    async def _execute_node_mutation(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationExecutionOutcome:
        """topology exclusive 内领取、变更 node、写 terminal 与事件（同一事务）。"""
        # 锁序固定：topology exclusive → 至多一个 SQLite 写事务。
        async with self._gate.exclusive():
            try:
                with self._store.write_transaction() as connection:
                    # 领取、node 变更、终态 record 与事件 outbox 在**同一**写事务内
                    # 提交：整条 operation 恰好推进一次 catalog generation，因此
                    # receipt 上报的 committed_catalog_revision 事后必然等于 snapshot
                    # 读到的 revision（不再有「领取单独提交一次」造成的额外推进）。
                    claimed = self._claim(connection, record)
                    if claimed is None:
                        raise NavigationSkipped(record.operation_id)
                    parent_node_id = self._resolve_parent(connection, claimed)
                    expected_revision = self._resolve_expected_revision(
                        connection, claimed
                    )
                    affected, result_node_id = self._apply_node_change(
                        connection, claimed, parent_node_id, expected_revision
                    )
                    result_node_revision = self._node_revision(
                        connection, result_node_id
                    )
                    terminal = self._finish(
                        connection,
                        claimed,
                        state="committed",
                        result_node_id=result_node_id,
                        result_node_revision=result_node_revision,
                        affected=affected,
                    )
                return NavigationExecutionOutcome(record=terminal, settled=True)
            except NavigationSkipped:
                # 本轮被其它 owner 抢先：不是业务拒绝，绝不可在下面被归类为
                # RuntimeError 而误写成 rejected 终态。领取事务已整体回滚。
                raise
            except (KeyError, ValueError, RuntimeError) as error:
                return self._reject(record, error)

    def _apply_node_change(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
        parent_node_id: str | None,
        expected_revision: int | None,
    ) -> tuple[list[str], str]:
        """在同一事务内应用 node 变更；返回 (受影响 node ID, 结果 node ID)。"""
        if record.kind == "create_folder":
            created = self._store.create_folder(
                record.reserved_node_id,
                self._workspace_id,
                parent_node_id,
                str(record.params["name"]),
                connection=connection,
            )
            return [created.node_id], created.node_id
        if record.kind == "rename_node":
            updated = self._store.apply_navigation_mutation(
                record.target_node_id,
                expected_revision=expected_revision,
                new_display_name=str(record.params["name"]),
                connection=connection,
            )
            return [updated.node_id], updated.node_id
        if record.kind == "move_node":
            updated = self._store.apply_navigation_mutation(
                record.target_node_id,
                expected_revision=expected_revision,
                new_parent_node_id=parent_node_id,
                connection=connection,
            )
            affected = [updated.node_id]
            if parent_node_id is not None:
                affected.append(parent_node_id)
            return affected, updated.node_id
        raise RuntimeError(f"executor 不处理的 operation kind: {record.kind}")

    # ------------------------------------------------------------------
    # 删除类 operation：复用 NavigationSubtreeDeleteRecord 协议
    # ------------------------------------------------------------------

    def _claim_or_raise(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationMutationRecord:
        """领取 operation（独立短事务）；未取得执行权时抛 NavigationSkipped。

        仅删除类 operation 使用：删除流自身的 mark 事务才是导航逻辑的 committed
        点，且它会取 topology exclusive 并运行异步 drain，无法与 SQLite 写事务
        合并，因此「先领取 → 再进删除流 → 最后单独写终态」是这一条链路的既定
        形态（在报告中显式标注为相对 8.1-G 单事务要求的偏差）。普通 node mutation
        不经过本方法，它们在单事务内领取。
        """
        with self._store.write_transaction() as connection:
            claimed = self._claim(connection, record)
        if claimed is None:
            raise NavigationSkipped(record.operation_id)
        return claimed

    async def _execute_delete(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationExecutionOutcome:
        """递归删除：delete service 的 mark 事务即导航逻辑 committed 点。

        删除流自身取 topology exclusive 与 Session gate，本处不得再持 gate
        （避免双 gate / 反向锁序）。physical settlement 独立于逻辑提交上报。
        """
        claimed = self._claim_or_raise(record)
        try:
            if self._delete_runner is None:
                result = await self._path_resolver.delete_subtree(
                    idempotency_key=claimed.operation_id,
                    root_node_id=claimed.target_node_id,
                )
            else:
                result = await self._delete_runner(
                    claimed.operation_id, claimed.target_node_id
                )
        except (KeyError, ValueError, RuntimeError) as error:
            return self._reject(claimed, error, claim=False)
        affected = [claimed.target_node_id, *result.frozen_node_ids]
        settled = result.record_state == "completed"
        with self._store.write_transaction() as connection:
            # 本 owner 已持有该 operation（``running`` + 本 token）；这里不再领取，
            # 只用 token CAS 写终态：若中途被接管，写事务会被拒绝而不是覆盖新 owner。
            live = self._require_live(connection, claimed)
            if live is None:
                return self._terminal_outcome(claimed)
            terminal = self._finish(
                connection,
                live,
                state="committed",
                result_node_id=claimed.target_node_id,
                affected=affected,
                pending_settlement=not settled,
            )
        return NavigationExecutionOutcome(record=terminal, settled=settled)

    # ------------------------------------------------------------------
    # 拒绝路径
    # ------------------------------------------------------------------

    def _reject(
        self,
        record: NavigationMutationRecord,
        error: Exception,
        *,
        claim: bool = True,
    ) -> NavigationExecutionOutcome:
        """明确拒绝：只写 terminal reason 与事件，node 变更随事务回滚。

        ``claim=True`` 表示调用方的 node 变更事务已整体回滚、目标仍是 ``queued``，
        需在本事务内重新领取后写终态；``claim=False`` 表示调用方已持有该
        operation（删除流），只重读并用 token 写终态。两者都在单事务内完成。
        """
        with self._store.write_transaction() as connection:
            if claim:
                claimed = self._claim(connection, record)
                if claimed is None:
                    existing = self._queue.fetch_record_in(
                        connection,
                        gateway_id=record.gateway_id,
                        workspace_id=record.workspace_id,
                        actor=record.actor,
                        operation_id=record.operation_id,
                    )
                    if existing is None:
                        raise KeyError(
                            f"会话目录 operation 不存在: {record.operation_id}"
                        )
                    if existing.is_terminal:
                        return self._terminal_outcome(existing)
                    # 已被其它 owner 领取：本轮不写终态（否则会覆盖新 owner 的
                    # 执行结果），交由调用方按 NavigationSkipped 跳过。
                    raise NavigationSkipped(record.operation_id)
            else:
                claimed = self._require_live(connection, record)
                if claimed is None:
                    return self._terminal_outcome(record)
            terminal = self._finish(
                connection,
                claimed,
                state="rejected",
                result_node_id=None,
                affected=_affected_on_reject(claimed),
                error_code=_error_code(error),
                error_detail=str(error),
            )
            successors = self._queue.mark_dependency_failed_successors(
                connection,
                workspace_id=self._workspace_id,
                failed_operation_id=terminal.operation_id,
                now=utc_now_text(),
            )
            for successor in successors:
                failed = self._queue.fetch_record_in(
                    connection,
                    gateway_id=successor.gateway_id,
                    workspace_id=successor.workspace_id,
                    actor=successor.actor,
                    operation_id=successor.operation_id,
                )
                self._queue.append_event(
                    connection,
                    record=failed,
                    affected_node_ids=_affected_on_reject(failed),
                    now=utc_now_text(),
                )
        return NavigationExecutionOutcome(record=terminal, settled=True)

    # ------------------------------------------------------------------
    # 事务内原语
    # ------------------------------------------------------------------

    def _claim(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
    ) -> NavigationMutationRecord | None:
        """事务内领取：重读后仅 ``queued → running``；否则返回 None。

        领取与终局写入在同一事务提交，因此并发 owner 只有一个能成功；另一个
        CAS 失败返回 None 并跳过（不重排队列顺序，也不重复应用副作用）。
        """
        current = self._queue.fetch_record_in(
            connection,
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=record.operation_id,
        )
        if current is None:
            raise KeyError(f"会话目录 operation 不存在: {record.operation_id}")
        if current.is_terminal:
            return None
        if current.state == "running":
            # 上一 owner 仍在执行，或崩溃遗留尚未被 recover_in_flight 重置：
            # 本轮不接管，避免两个 owner 同时执行同一条 operation。
            return None
        return self._queue.claim_running(
            connection,
            record=current,
            holder_id="worker",
            now=utc_now_text(),
        )

    def _finish(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
        *,
        state: str,
        result_node_id: str | None,
        affected: list[str],
        result_node_revision: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        pending_settlement: bool = False,
    ) -> NavigationMutationRecord:
        """在同一事务内写 terminal record 与导航事件 outbox。"""
        terminal = self._queue.finish_terminal(
            connection,
            record=record,
            expected_fencing_token=record.fencing_token,
            state=state,
            now=utc_now_text(),
            result_node_id=result_node_id,
            result_node_revision=result_node_revision,
            committed_catalog_revision=self._committed_revision(connection),
            error_code=error_code,
            error_detail=error_detail,
            pending_settlement=pending_settlement,
        )
        self._queue.append_event(
            connection,
            record=terminal,
            affected_node_ids=affected,
            now=utc_now_text(),
        )
        return terminal

    @staticmethod
    def _terminal_outcome(record: NavigationMutationRecord) -> NavigationExecutionOutcome:
        return NavigationExecutionOutcome(
            record=record, settled=not record.pending_settlement
        )

    def _require_live(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
    ) -> NavigationMutationRecord | None:
        """重读同一 operation；返回 None 表示已终态（幂等 no-op）。"""
        current = self._queue.fetch_record_in(
            connection,
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=record.operation_id,
        )
        if current is None:
            raise KeyError(f"会话目录 operation 不存在: {record.operation_id}")
        return None if current.is_terminal else current

    def _resolve_expected_revision(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
    ) -> int | None:
        """解析执行期 CAS 前置 revision。

        同 node 连续编辑（依赖/``created_by_operation_id``）以前序 operation 的**已
        提交结果 revision** 为前置，而不是客户端入队时的旧 revision：这样「同一
        客户端连续改名/移动」不会因自身前一条命令推进了 revision 而误判冲突，而
        其它客户端的并发修改仍会让 CAS 失败并明确拒绝。
        """
        if record.kind == "create_folder":
            return None
        dependency = self._predecessor_for(connection, record)
        if dependency is not None and dependency.result_node_id == record.target_node_id:
            if dependency.result_node_revision is None:
                raise RuntimeError(
                    "前序 operation 缺少结果 revision，无法串行同 node 编辑: "
                    f"operation={record.operation_id}, "
                    f"predecessor={dependency.operation_id}"
                )
            return dependency.result_node_revision
        value = record.params.get("expected_revision")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                "operation 缺少 expected_revision，无法执行 CAS: "
                f"operation_id={record.operation_id}"
            )
        return value

    def _predecessor_for(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
    ) -> NavigationMutationRecord | None:
        """返回同 node 编辑链上的直接前序 operation（无则 None）。

        优先级：显式 ``depends_on`` 中结果节点命中目标 node 的最近一条；否则
        ``created_by_operation_id`` 引用。
        """
        candidates: list[NavigationMutationRecord] = []
        for dependency_id in record.depends_on:
            referenced = self._queue.fetch_record_in(
                connection,
                gateway_id=record.gateway_id,
                workspace_id=record.workspace_id,
                actor=record.actor,
                operation_id=dependency_id,
            )
            if referenced is not None:
                candidates.append(referenced)
        if record.created_by_operation_id is not None:
            referenced = self._queue.fetch_record_in(
                connection,
                gateway_id=record.gateway_id,
                workspace_id=record.workspace_id,
                actor=record.actor,
                operation_id=record.created_by_operation_id,
            )
            if referenced is not None:
                candidates.append(referenced)
        matching = [
            candidate
            for candidate in candidates
            if candidate.result_node_id == record.target_node_id
        ]
        if not matching:
            return None
        return max(matching, key=lambda item: item.queue_seq)

    @staticmethod
    def _node_revision(connection: sqlite3.Connection, node_id: str) -> int:
        row = connection.execute(
            "SELECT revision FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"会话目录节点不存在: {node_id}")
        return int(row[0])

    def _resolve_parent(
        self,
        connection: sqlite3.Connection,
        record: NavigationMutationRecord,
    ) -> str | None:
        """解析实际父节点：跨 operation 的 ``created_by_operation_id`` 或显式值。

        ``created_by_operation_id`` 只在同一认证 scope 内解析；被引用的 operation
        必须已 committed 并给出 ``result_node_id``，否则明确拒绝（不猜测、不重基）。
        """
        created_by = record.created_by_operation_id
        if created_by is None:
            parent = record.params.get("parent_node_id")
            return parent if isinstance(parent, str) else None
        referenced = self._queue.fetch_record_in(
            connection,
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=created_by,
        )
        if referenced is None:
            raise KeyError(
                "created_by_operation_id 引用的 operation 不存在: "
                f"operation={record.operation_id}, referenced={created_by}"
            )
        if referenced.state != "committed" or referenced.result_node_id is None:
            raise RuntimeError(
                "created_by_operation_id 尚未成功提交，无法解析父节点: "
                f"operation={record.operation_id}, referenced={created_by}, "
                f"referenced_state={referenced.state}"
            )
        return referenced.result_node_id

    @staticmethod
    def _committed_revision(connection: sqlite3.Connection) -> int:
        """本次写事务提交后的 catalog revision（与队列层共用同一读取口径）。"""
        return read_catalog_revision(connection, committed=True)


def _error_code(error: Exception) -> str:
    if isinstance(error, SessionDeletionPendingError):
        return "session_deletion_pending"
    if isinstance(error, SourceRetainedByForkError):
        return "source_retained_by_fork"
    if isinstance(error, SourceRetentionOperationPendingError):
        return "source_retention_operation_pending"
    if isinstance(error, KeyError):
        return "node_not_found"
    if isinstance(error, ValueError):
        return "invalid_operation"
    return "conflict"


def _affected_on_reject(record: NavigationMutationRecord) -> list[str]:
    affected: list[str] = []
    if record.target_node_id is not None:
        affected.append(record.target_node_id)
    parent = record.params.get("parent_node_id")
    if isinstance(parent, str):
        affected.append(parent)
    return affected
