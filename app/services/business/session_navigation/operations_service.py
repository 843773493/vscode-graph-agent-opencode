"""会话目录异步 mutation 的公共业务面（OpenSpec 8.1-G/8.1-H）。

本模块把「typed 批量入队 → fenced worker 执行 → 按 ID 状态查询 / revision-pinned
snapshot / navigation 事件 cursor」暴露为**唯一**目录写协议，替代原先的多个同步
写端点。写操作本身全部委托 :class:`NavigationMutationExecutor`，因此不存在第二套
写入实现。

关键契约：

- ``enqueue`` 只做短 SQLite 工作（鉴权、幂等、单调 queue_seq、Folder ID 预留、
  record 持久化），**不**取 topology gate 做整树预检，也不等文件/网络/模型；
- ``202`` 只表示 durable acceptance，绝不表示目录已改变；
- ``operation_id`` 等于经验证的 ``client_operation_id``；``(gateway, workspace,
  actor, operation_id)`` 唯一，同 key 同 preimage 幂等、异 preimage 冲突；
- terminal 后保留 compact tombstone，迟到重放返回原 receipt/terminal；
- 事件 ``event_seq`` 与 terminal record 在同一 catalog 事务提交，cursor 可恢复。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.core.sqlite_state import utc_now_text
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationEventDTO,
    NavigationEventsPageDTO,
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationReceiptDTO,
    NavigationMutationStatusPageDTO,
    NavigationSnapshotDTO,
)
from app.services.business.session_navigation.executor import (
    NavigationExecutionOutcome,
    NavigationMutationExecutor,
)
from app.services.business.session_navigation.queue_store import (
    NavigationEventRecord,
    NavigationMutationQueueStore,
    NavigationMutationRecord,
    NavigationQueueOwnerError,
    read_catalog_revision,
)

__all__ = [
    "LOCAL_ACTOR",
    "LOCAL_GATEWAY_ID",
    "NavigationAuthScope",
    "SessionCatalogOperationsService",
    "local_navigation_scope",
]

# 每次 SSE/轮询事件页的默认上限。
_DEFAULT_EVENT_LIMIT = 200
# 同步 façade 等待 operation 终态的有界窗口与轮询间隔。
_TERMINAL_WAIT_TIMEOUT_SECONDS = 30.0
_TERMINAL_POLL_INTERVAL_SECONDS = 0.02
# 应用启动必须在有限时间内取得 owner 并完成 durable recovery。
_OWNER_READY_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger(__name__)

# 本地工作区后端的认证主体。当前架构没有云端控制面，工作区后端只经本地 token
# 被同机 Gateway 访问，因此不存在可区分的第二主体；gateway_id/actor 取本地固定
# 身份，workspace 取后端自身身份（永不信任请求体自报）。
# TODO: 联邦/多主体接入时，改由 Gateway 在认证后透传 peer gateway 与用户身份。
LOCAL_GATEWAY_ID = "local"
LOCAL_ACTOR = "local"


@dataclass(frozen=True, slots=True)
class NavigationAuthScope:
    """已认证路由给出的 operation 幂等 scope（不信任客户端自报）。

    ``(gateway_id, workspace_id, actor, client_operation_id)`` 唯一：同 scope 同 key
    幂等、异 preimage 冲突。``workspace_id`` 必须等于本工作区后端身份。
    """

    gateway_id: str
    workspace_id: str
    actor: str


def local_navigation_scope(workspace_id: str) -> NavigationAuthScope:
    """构造本地工作区后端的导航 operation 幂等 scope。"""
    return NavigationAuthScope(
        gateway_id=LOCAL_GATEWAY_ID,
        workspace_id=workspace_id,
        actor=LOCAL_ACTOR,
    )


class SessionCatalogOperationsService:
    """typed 导航 operation 的 enqueue、执行与观测面。"""

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        workspace_id: str,
        queue: NavigationMutationQueueStore,
        executor: NavigationMutationExecutor,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        self._store = store
        self._workspace_id = workspace_id
        self._queue = queue
        self._executor = executor
        # worker 由应用 lifespan 显式启动和停止；请求入口不得临时创建第二种 owner。
        self._worker_task: asyncio.Task[None] | None = None
        self._worker_state = "stopped"
        self._worker_error: BaseException | None = None
        self._worker_ready = asyncio.Event()

    # ------------------------------------------------------------------
    # 进程内唯一执行 owner
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        owner_timeout_seconds: float = _OWNER_READY_TIMEOUT_SECONDS,
    ) -> None:
        """等待唯一 owner 取得锁并完成恢复；冲突超时使应用启动明确失败。"""
        if owner_timeout_seconds <= 0:
            raise ValueError(
                "导航 queue owner readiness timeout 必须为正数: "
                f"{owner_timeout_seconds}"
            )
        if self._worker_error is not None:
            raise self._worker_failure_error() from self._worker_error
        task = self._worker_task
        if task is None or task.done():
            self._worker_ready = asyncio.Event()
            self._worker_state = "starting"
            task = asyncio.create_task(run_worker(self))
            self._worker_task = task
            task.add_done_callback(self._observe_worker_exit)
        try:
            await asyncio.wait_for(
                self._worker_ready.wait(),
                timeout=owner_timeout_seconds,
            )
        except asyncio.CancelledError:
            self._worker_state = "stopping"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                if self._worker_task is task:
                    self._worker_task = None
                if self._worker_error is None:
                    self._worker_state = "stopped"
            raise
        except TimeoutError as error:
            detail = (
                "等待导航 queue owner 锁与启动恢复超时，拒绝以未就绪状态启动: "
                f"workspace_id={self._workspace_id}, state={self._worker_state}, "
                f"timeout_seconds={owner_timeout_seconds}"
            )
            conflict = NavigationQueueOwnerError(detail)
            self._worker_state = "stopping"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                if self._worker_task is task:
                    self._worker_task = None
            self._worker_error = conflict
            self._worker_state = "failed"
            raise self._worker_failure_error() from error
        if self._worker_error is not None:
            raise self._worker_failure_error() from self._worker_error
        if self._worker_state != "running":
            error = NavigationQueueOwnerError(
                "导航 queue worker 在 ready 前退出: "
                f"workspace_id={self._workspace_id}, state={self._worker_state}"
            )
            self._worker_error = error
            self._worker_state = "failed"
            raise self._worker_failure_error() from error

    async def stop(self) -> None:
        """取消并等待 worker 退出，确保其释放 queue owner OS lock。"""
        task = self._worker_task
        if task is None:
            if self._worker_error is None:
                self._worker_state = "stopped"
            return
        if not task.done():
            self._worker_state = "stopping"
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            if self._worker_error is None:
                self._worker_error = error
                self._worker_state = "failed"
                logger.error(
                    "导航 queue worker 失败，工作区操作不再被后台执行: workspace_id=%s",
                    self._workspace_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
            elif error is not self._worker_error:
                raise
            logger.debug("已观察并记录导航 queue worker 后台错误: %s", error)
        finally:
            if self._worker_task is task:
                self._worker_task = None
            if self._worker_error is None:
                self._worker_state = "stopped"

    @property
    def worker_state(self) -> str:
        """返回 owner 生命周期状态。"""
        return self._worker_state

    @property
    def worker_error(self) -> str | None:
        """返回后台 worker 最近一次失败的详细异常。"""
        return None if self._worker_error is None else repr(self._worker_error)

    def _observe_worker_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            if self._worker_state not in ("stopping", "failed"):
                error = RuntimeError("导航 queue owner 在 lifespan 之外被取消")
                self._worker_error = error
                self._worker_state = "failed"
                logger.error("导航 queue worker 异常取消: %s", error)
            self._worker_ready.set()
            return
        error = task.exception()
        if error is None:
            error = RuntimeError("导航 queue worker 意外退出")
        self._worker_error = error
        self._worker_state = "failed"
        logger.error(
            "导航 queue worker 失败，工作区操作不再被后台执行: workspace_id=%s",
            self._workspace_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._worker_ready.set()

    def _worker_failure_error(self) -> NavigationQueueOwnerError:
        detail = repr(self._worker_error)
        return NavigationQueueOwnerError(
            "导航 queue worker 已失败，工作区导航队列不可用: "
            f"workspace_id={self._workspace_id}, error={detail}"
        )

    def _require_worker_ready(self) -> None:
        task = self._worker_task
        if task is not None and task.done() and self._worker_error is None:
            self._observe_worker_exit(task)
        if self._worker_error is not None:
            raise self._worker_failure_error() from self._worker_error
        if (
            task is None
            or task.done()
            or self._worker_state != "running"
        ):
            raise NavigationQueueOwnerError(
                "导航 queue worker 尚未取得 owner 并完成启动恢复: "
                f"workspace_id={self._workspace_id}, state={self._worker_state}"
            )

    # ------------------------------------------------------------------
    # 入队（短事务，202 durable acceptance）
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        request: NavigationMutationEnqueueRequest,
        scope: NavigationAuthScope,
    ) -> NavigationMutationEnqueueResultDTO:
        """原子接受一批 intent 并返回 202 receipt（不执行任何目录变更）。"""
        self._require_worker_ready()
        self._require_scope(scope)
        now = utc_now_text()
        with self._store.write_transaction() as connection:
            records = self._queue.enqueue_batch(
                connection,
                gateway_id=scope.gateway_id,
                workspace_id=self._workspace_id,
                actor=scope.actor,
                intents=request.intents,
                now=now,
            )
        created_node_ids = {
            record.operation_id: record.reserved_node_id
            for record in records
            if record.reserved_node_id is not None
        }
        # 202 已是 durable acceptance；已由 lifespan 启动的 owner 异步排空，不在
        # 此等待执行结果，也不把执行结果当成功展示。
        return NavigationMutationEnqueueResultDTO(
            workspace_id=self._workspace_id,
            accepted_count=len(records),
            receipts=[self._to_receipt(record) for record in records],
            created_node_ids=created_node_ids,
        )

    async def await_terminal(
        self,
        operation_id: str,
        scope: NavigationAuthScope,
    ) -> NavigationMutationRecord:
        """只轮询 durable 状态，不从 API caller 绕过唯一 worker 执行队列。

        超时是明确错误（可能被前置依赖阻塞），绝不静默当作成功。
        """
        self._require_worker_ready()
        deadline = asyncio.get_running_loop().time() + _TERMINAL_WAIT_TIMEOUT_SECONDS
        while True:
            self._require_worker_ready()
            record = self._queue.get_record(
                gateway_id=scope.gateway_id,
                workspace_id=self._workspace_id,
                actor=scope.actor,
                operation_id=operation_id,
            )
            if record is None:
                raise KeyError(f"会话目录 operation 未被 durable 接受: {operation_id}")
            if record.is_terminal:
                return record
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "会话目录 operation 未在等待窗口内到达终态（可能被前置依赖阻塞）: "
                    f"operation_id={operation_id}, state={record.state}"
                )
            await asyncio.sleep(_TERMINAL_POLL_INTERVAL_SECONDS)

    def node_revision(self, node_id: str) -> int:
        """读取权威 nodes 行的当前 revision（同步 façade 构造 CAS 前置）。"""
        with self._store.read_transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"会话目录节点不存在: {node_id}")
            return int(row[0])

    def node_kind(self, node_id: str) -> str:
        """读取权威 nodes 行的 kind。"""
        with self._store.read_transaction() as connection:
            row = connection.execute(
                "SELECT kind FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"会话目录节点不存在: {node_id}")
            return str(row[0])

    def record(
        self,
        operation_id: str,
        scope: NavigationAuthScope,
    ) -> NavigationMutationRecord:
        """按精确 ID 返回单条 durable record；不存在抛 KeyError。"""
        self._require_scope(scope)
        record = self._queue.get_record(
            gateway_id=scope.gateway_id,
            workspace_id=self._workspace_id,
            actor=scope.actor,
            operation_id=operation_id,
        )
        if record is None:
            raise KeyError(f"会话目录 operation 不存在: {operation_id}")
        return record

    # ------------------------------------------------------------------
    # 按 ID 状态查询
    # ------------------------------------------------------------------

    def status(
        self,
        operation_ids: list[str],
        scope: NavigationAuthScope,
    ) -> NavigationMutationStatusPageDTO:
        """按精确 ID 返回 durable 状态；未知 ID 显式列出而不当作失败。"""
        self._require_worker_ready()
        self._require_scope(scope)
        records, unknown = self._queue.list_records(
            gateway_id=scope.gateway_id,
            workspace_id=self._workspace_id,
            actor=scope.actor,
            operation_ids=operation_ids,
        )
        return NavigationMutationStatusPageDTO(
            workspace_id=self._workspace_id,
            catalog_revision=self._catalog_revision(),
            items=[self._to_receipt(record) for record in records],
            unknown_operation_ids=unknown,
        )

    # ------------------------------------------------------------------
    # snapshot 与事件
    # ------------------------------------------------------------------

    def snapshot(self) -> NavigationSnapshotDTO:
        """在只读单事务内取同一 revision 与事件水位。"""
        self._require_worker_ready()
        with self._store.read_transaction() as connection:
            revision = self._revision_in(connection)
            watermark = self._queue.event_watermark_in(connection, self._workspace_id)
            generation = revision
        return NavigationSnapshotDTO(
            workspace_id=self._workspace_id,
            catalog_revision=revision,
            event_seq_watermark=watermark,
            generation=generation,
        )

    def events(
        self,
        *,
        after: int,
        limit: int | None = None,
    ) -> NavigationEventsPageDTO:
        """返回 ``event_seq > after`` 的终态事件页与可恢复 cursor。"""
        self._require_worker_ready()
        if after < 0:
            raise ValueError(f"navigation 事件 cursor 不能为负: {after}")
        resolved_limit = limit if limit is not None else _DEFAULT_EVENT_LIMIT
        if resolved_limit < 1:
            raise ValueError(f"navigation 事件页上限必须为正: {resolved_limit}")
        records, watermark = self._queue.list_events(
            workspace_id=self._workspace_id,
            after=after,
            limit=resolved_limit + 1,
        )
        has_more = len(records) > resolved_limit
        page = records[:resolved_limit]
        return NavigationEventsPageDTO(
            workspace_id=self._workspace_id,
            event_seq_watermark=watermark,
            items=[self._to_event(record) for record in page],
            next_cursor=(
                self._encode_cursor(page[-1].event_seq, watermark)
                if has_more and page
                else None
            ),
            has_more=has_more,
        )

    def decode_events_cursor(self, cursor: str) -> tuple[int, int]:
        """解析事件 cursor：返回 (after, 签发时水位)。

        cursor 编码的是「已经消费到的 ``event_seq``」与该时刻水位；两者都只用于
        定位，不构成授权。重复/乱序事件由客户端按 ``event_seq`` + ``operation_id``
        去重，本方法不保证去重。
        """
        self._require_worker_ready()
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("navigation 事件 cursor 无效") from error
        if not isinstance(payload, dict):
            raise TypeError("navigation 事件 cursor 格式无效")
        after = payload.get("after")
        watermark = payload.get("watermark")
        if not isinstance(after, int) or after < 0:
            raise ValueError("navigation 事件 cursor after 无效")
        if not isinstance(watermark, int) or watermark < 0:
            raise ValueError("navigation 事件 cursor watermark 无效")
        return after, watermark

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    async def drain_once(self) -> list[NavigationExecutionOutcome]:
        """在唯一 workspace owner 生命周期内执行一次 FIFO 排空。"""
        return await self._executor.drain()

    @asynccontextmanager
    async def worker_owner(self):
        """持有 workspace owner；等待其它有效 owner 时保留可取消状态。"""
        self._worker_state = "waiting_for_owner"
        logger.info(
            "导航 queue worker 等待 workspace owner lock: workspace_id=%s",
            self._workspace_id,
        )
        async with self._executor.queue_owner():
            self._worker_state = "recovering"
            logger.info(
                "导航 queue worker 已取得 workspace owner lock: workspace_id=%s",
                self._workspace_id,
            )
            yield

    async def recover_after_restart(self) -> int:
        """新 owner 持锁后重置旧 generation 的 ``running`` 行。"""
        return await self._executor.recover_in_flight()

    async def recover_pending_subtree_deletes(self) -> list[SubtreeDeleteResult]:
        """持有 queue owner 后恢复没有待执行队列记录的子树删除。"""
        return await self._executor.recover_pending_subtree_deletes()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _catalog_revision(self) -> int:
        with self._store.read_transaction() as connection:
            return self._revision_in(connection)

    def _require_scope(self, scope: NavigationAuthScope) -> None:
        """范围必须与本工作区一致：workspace 由后端身份决定，不信任请求体。"""
        if scope.workspace_id != self._workspace_id:
            raise ValueError(
                "导航 operation 的 workspace 与当前工作区不一致: "
                f"scope={scope.workspace_id}, workspace={self._workspace_id}"
            )

    @staticmethod
    def _revision_in(connection) -> int:
        """只读快照口径的 catalog revision（与队列/执行器共用同一读取实现）。"""
        return read_catalog_revision(connection, committed=False)

    @staticmethod
    def _encode_cursor(after: int, watermark: int) -> str:
        payload = json.dumps(
            {"after": after, "watermark": watermark},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _to_receipt(record: NavigationMutationRecord) -> NavigationMutationReceiptDTO:
        return NavigationMutationReceiptDTO(
            operation_id=record.operation_id,
            client_sequence=record.client_sequence,
            queue_seq=record.queue_seq,
            kind=record.kind,
            state=record.state,
            created_node_id=record.result_node_id or record.reserved_node_id,
            committed_catalog_revision=record.committed_catalog_revision,
            error_code=record.error_code,
            error_detail=record.error_detail,
            pending_settlement=record.pending_settlement,
            receipt_revision=record.receipt_revision,
            updated_at=datetime.fromisoformat(record.updated_at),
        )

    @staticmethod
    def _to_event(record: NavigationEventRecord) -> NavigationEventDTO:
        return NavigationEventDTO(
            event_seq=record.event_seq,
            workspace_id=record.workspace_id,
            operation_id=record.operation_id,
            queue_seq=record.queue_seq,
            kind=record.kind,
            result_state=record.result_state,
            committed_catalog_revision=record.committed_catalog_revision,
            affected_node_ids=list(record.affected_node_ids),
            error_code=record.error_code,
            error_detail=record.error_detail,
            created_at=datetime.fromisoformat(record.created_at),
        )


async def run_worker(
    service: SessionCatalogOperationsService,
    *,
    poll_interval_seconds: float = 0.5,
) -> None:
    """以轮询方式持续排空队列的后台 worker（进程内唯一 owner）。

    持有跨进程 workspace owner lock 直至 worker 退出；拿到锁后递增 durable
    owner generation，再恢复旧 ``running`` 并按 FIFO 排空。其它进程等待 owner
    锁期间不能 recovery 或执行。worker 与所属事件循环同生命周期，应用关停时由
    事件循环取消。
    """
    async with service.worker_owner():
        await service.recover_after_restart()
        recovered_deletes = await service.recover_pending_subtree_deletes()
        if recovered_deletes:
            logger.warning("已恢复 %s 条未终结的会话子树删除", len(recovered_deletes))
        service._worker_state = "running"
        logger.info("导航 queue worker 恢复完成并开始排空: workspace_id=%s", service._workspace_id)
        service._worker_ready.set()
        while True:
            outcomes = await service.drain_once()
            if outcomes:
                continue
            await asyncio.sleep(poll_interval_seconds)
