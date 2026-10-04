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

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCatalogStore,
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
)
from app.core.session_catalog_store.contracts import (
    CatalogTransactionHook,
    SubtreeDeleteRecord,
)
from app.core.session_lifecycle_gate import (
    NavigationMutationQueueOwnerGate,
    NavigationTopologyGate,
    SessionDeletionPendingError,
)
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.core.sqlite_state import utc_now_text
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationMutationRecord,
    NavigationQueueOwner,
    NavigationQueueOwnerError,
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
DeleteRunner = Callable[
    [str, str, CatalogTransactionHook, CatalogTransactionHook],
    Awaitable[SubtreeDeleteResult],
]
logger = logging.getLogger(__name__)


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
        self._queue_owner_gate = NavigationMutationQueueOwnerGate(store.sessions_root)
        self._owner: NavigationQueueOwner | None = None

    @asynccontextmanager
    async def queue_owner(self):
        """持有 workspace owner OS 锁并建立本次 worker generation。"""
        async with self._queue_owner_gate.exclusive():
            if self._owner is not None:
                raise NavigationQueueOwnerError(
                    "同一 executor 不能重复取得 queue owner"
                )
            with self._store.write_transaction() as connection:
                owner = self._queue.acquire_owner(
                    connection,
                    workspace_id=self._workspace_id,
                    owner_id=uuid4().hex,
                    now=utc_now_text(),
                )
            self._owner = owner
            try:
                yield owner
            finally:
                self._owner = None

    def _require_owner(self) -> NavigationQueueOwner:
        if self._owner is None:
            raise NavigationQueueOwnerError(
                "导航 queue 执行缺少持锁的 workspace owner"
            )
        return self._owner

    @property
    def has_queue_owner(self) -> bool:
        """当前 executor 是否处于持锁的 worker owner 生命周期。"""
        return self._owner is not None

    # ------------------------------------------------------------------
    # 队列驱动
    # ------------------------------------------------------------------

    async def execute_next(self) -> NavigationExecutionOutcome | None:
        """执行下一条 runnable operation；None 表示队列已无可执行项。

        只有最早非终态 operation 可以成为 candidate；claim 若发现队首变化，本轮
        不跳到后继项。
        """
        self._require_owner()
        with self._store.read_transaction() as connection:
            candidate = self._queue.next_runnable(connection, self._workspace_id)
        if candidate is None:
            return None
        try:
            return await self.execute_record(candidate)
        except NavigationSkipped:
            return None

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
        """持有 OS owner lock 后按新 generation 接管旧 worker 的 running 行。"""
        owner = self._require_owner()
        with self._store.write_transaction() as connection:
            return self._queue.recover_in_flight(
                connection,
                workspace_id=self._workspace_id,
                owner_id=owner.owner_id,
                owner_generation=owner.generation,
                now=utc_now_text(),
            )

    async def recover_pending_subtree_deletes(self) -> list[SubtreeDeleteResult]:
        """恢复不再由 queued/running 导航 operation 驱动的删除 settlement。"""
        self._require_owner()
        recovered: list[SubtreeDeleteResult] = []
        for delete_record in self._path_resolver.pending_subtree_deletes():
            with self._store.read_transaction() as connection:
                operation = self._queue.find_delete_operation_in(
                    connection,
                    workspace_id=self._workspace_id,
                    operation_id=delete_record.subtree_delete_idempotency_key,
                )
            if operation is not None and operation.state in ("queued", "running"):
                # 仍按 queue_seq 执行；启动恢复不得越过前置 operation。
                continue
            if operation is not None and operation.state != "committed":
                raise RuntimeError(
                    "待恢复的子树删除对应非 committed 导航终态，拒绝改写 tombstone: "
                    f"operation_id={operation.operation_id}, state={operation.state}"
                )
            recovered.append(
                await self._path_resolver.delete_subtree(
                    idempotency_key=delete_record.subtree_delete_idempotency_key,
                    root_node_id=delete_record.root_node_id,
                    mark_transaction_hook=self._subtree_delete_mark_hook,
                    finish_transaction_hook=self._subtree_delete_finish_hook,
                )
            )
        return recovered

    async def execute_record(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationExecutionOutcome:
        """执行单条已 durable 接受的 operation（终态为幂等 no-op）。

        未取得执行权时抛 :class:`NavigationSkipped`（不是假终态）：调用方据此
        区分「已完成」与「本轮被其它 owner 抢先」。
        """
        self._require_owner()
        if record.is_terminal:
            return NavigationExecutionOutcome(record=record, settled=True)
        if record.kind == "delete_folder" and not bool(
            record.params.get("recursive", False)
        ):
            return await self._execute_empty_folder_delete(record)
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
            except NavigationQueueOwnerError:
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

    async def _execute_empty_folder_delete(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationExecutionOutcome:
        """在唯一队列事务内删除空 folder；非空 folder 明确拒绝。"""
        async with self._gate.exclusive():
            try:
                with self._store.write_transaction() as connection:
                    claimed = self._claim(connection, record)
                    if claimed is None:
                        raise NavigationSkipped(record.operation_id)
                    self._store.delete_empty_folder(
                        str(claimed.target_node_id), connection=connection
                    )
                    terminal = self._finish(
                        connection,
                        claimed,
                        state="committed",
                        result_node_id=claimed.target_node_id,
                        affected=[str(claimed.target_node_id)],
                    )
                return NavigationExecutionOutcome(record=terminal, settled=True)
            except NavigationSkipped:
                raise
            except NavigationQueueOwnerError:
                raise
            except (KeyError, ValueError, RuntimeError) as error:
                rejection = (
                    ValueError(str(error)) if isinstance(error, RuntimeError) else error
                )
                return self._reject(record, rejection)

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
        （避免双 gate / 反向锁序）。mark transaction hook 把导航 committed 事实
        与 catalog deleting 原子提交；physical settlement 独立上报。
        """
        claimed = self._claim_or_raise(record)
        try:
            if self._delete_runner is None:
                result = await self._path_resolver.delete_subtree(
                    idempotency_key=claimed.operation_id,
                    root_node_id=claimed.target_node_id,
                    mark_transaction_hook=self._subtree_delete_mark_hook,
                    finish_transaction_hook=self._subtree_delete_finish_hook,
                )
            else:
                result = await self._delete_runner(
                    claimed.operation_id,
                    claimed.target_node_id,
                    self._subtree_delete_mark_hook,
                    self._subtree_delete_finish_hook,
                )
        except (KeyError, ValueError, RuntimeError) as error:
            committed = self._read_committed_delete(claimed)
            if committed is None:
                return self._reject(claimed, error, claim=False)
            logger.error(
                "递归删除已逻辑提交但物理 settlement 失败，保留 pending 状态: "
                "operation_id=%s, error=%s",
                claimed.operation_id,
                error,
            )
            return self._terminal_outcome(committed)
        except Exception:
            committed = self._read_committed_delete(claimed)
            if committed is None:
                raise
            logger.exception(
                "递归删除已逻辑提交但物理 settlement 异常，保留 pending 状态: "
                "operation_id=%s",
                claimed.operation_id,
            )
            return self._terminal_outcome(committed)

        settled = result.record_state == "completed"
        with self._store.write_transaction() as connection:
            current = self._queue.fetch_record_in(
                connection,
                gateway_id=claimed.gateway_id,
                workspace_id=claimed.workspace_id,
                actor=claimed.actor,
                operation_id=claimed.operation_id,
            )
            if current is None:
                raise KeyError(f"会话目录 operation 不存在: {claimed.operation_id}")
            if current.state == "committed" and settled:
                terminal = self._queue.settle_delete_operation_in(
                    connection,
                    record=current,
                    now=utc_now_text(),
                ) if current.pending_settlement else current
            elif current.state == "committed":
                terminal = current
            else:
                raise RuntimeError(
                    "递归删除完成后导航 operation 状态不一致: "
                    f"operation_id={claimed.operation_id}, state={current.state}"
                )
        return self._terminal_outcome(terminal)

    def _subtree_delete_mark_hook(
        self,
        connection: sqlite3.Connection,
        delete_record: SubtreeDeleteRecord,
    ) -> None:
        operation = self._queue.find_delete_operation_in(
            connection,
            workspace_id=self._workspace_id,
            operation_id=delete_record.subtree_delete_idempotency_key,
        )
        if operation is None:
            return
        if operation.target_node_id != delete_record.root_node_id:
            raise RuntimeError(
                "subtree delete root 与导航 operation target 不一致: "
                f"operation_id={operation.operation_id}, "
                f"target={operation.target_node_id}, root={delete_record.root_node_id}"
            )
        if operation.state == "committed":
            if operation.result_node_id != delete_record.root_node_id:
                raise RuntimeError(
                    "已 committed 的递归删除 receipt 与 catalog root 不一致: "
                    f"operation_id={operation.operation_id}"
                )
            return
        if operation.state != "running":
            raise RuntimeError(
                "子树删除 mark 只能提交当前 running 导航 operation: "
                f"operation_id={operation.operation_id}, state={operation.state}"
            )
        live = self._require_live(connection, operation)
        if live is None:
            raise RuntimeError(
                "子树删除 mark 时导航 operation 已终结: "
                f"operation_id={operation.operation_id}"
            )
        self._finish(
            connection,
            live,
            state="committed",
            result_node_id=delete_record.root_node_id,
            affected=[
                delete_record.root_node_id,
                *(item.node_id for item in delete_record.frozen_node_ids),
            ],
            pending_settlement=True,
        )

    def _subtree_delete_finish_hook(
        self,
        connection: sqlite3.Connection,
        delete_record: SubtreeDeleteRecord,
    ) -> None:
        operation = self._queue.find_delete_operation_in(
            connection,
            workspace_id=self._workspace_id,
            operation_id=delete_record.subtree_delete_idempotency_key,
        )
        if operation is None:
            return
        owner = self._require_owner()
        self._queue.require_owner_generation(
            connection,
            workspace_id=self._workspace_id,
            owner_id=owner.owner_id,
            generation=owner.generation,
        )
        if operation.state != "committed":
            raise RuntimeError(
                "子树删除完成时导航 operation 未由 mark 事务同步提交为 committed: "
                f"operation_id={operation.operation_id}, state={operation.state}"
            )
        if operation.pending_settlement:
            self._queue.settle_delete_operation_in(
                connection,
                record=operation,
                now=utc_now_text(),
            )

    def _read_committed_delete(
        self,
        record: NavigationMutationRecord,
    ) -> NavigationMutationRecord | None:
        current = self._queue.get_record(
            gateway_id=record.gateway_id,
            workspace_id=record.workspace_id,
            actor=record.actor,
            operation_id=record.operation_id,
        )
        if (
            current is None
            or current.state != "committed"
            or current.result_node_id != record.target_node_id
        ):
            return None
        return current

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
                error_detail=_rejection_detail(error),
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
            # running owner 在 OS lock 仍存活期间不可接管；只由已获新 generation
            # 的 worker 在 recover_in_flight 中恢复。
            return None
        owner = self._require_owner()
        return self._queue.claim_running(
            connection,
            record=current,
            holder_id=owner.holder_id,
            owner_id=owner.owner_id,
            owner_generation=owner.generation,
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
        owner = self._require_owner()
        self._queue.require_owner_generation(
            connection,
            workspace_id=self._workspace_id,
            owner_id=owner.owner_id,
            generation=owner.generation,
        )
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
        owner = self._require_owner()
        self._queue.require_owner_generation(
            connection,
            workspace_id=self._workspace_id,
            owner_id=owner.owner_id,
            generation=owner.generation,
        )
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


def _rejection_detail(error: Exception) -> str:
    """rejected record 的对外错误文本，不泄漏 Python repr。

    ``KeyError`` 的 ``str()`` 会给消息补一对引号；该文本既会经
    ``_raise_for_rejected`` 重新抛回同步 API，也会直接作为 durable 回执/事件的
    ``error_detail`` 下发。带引号的字面量一旦落库，适配层的 ``client_error_message``
    就无法再还原为原始消息，因此必须在唯一生产点取未加引号的消息本体；其余异常
    仍是 ``str(error)``。
    """
    if isinstance(error, KeyError) and error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def _affected_on_reject(record: NavigationMutationRecord) -> list[str]:
    affected: list[str] = []
    if record.target_node_id is not None:
        affected.append(record.target_node_id)
    parent = record.params.get("parent_node_id")
    if isinstance(parent, str):
        affected.append(parent)
    return affected
