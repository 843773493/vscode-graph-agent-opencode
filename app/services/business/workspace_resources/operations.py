"""单次受信调用的 lease 引用；不创建、操作或停止外部实例。"""

from __future__ import annotations

import asyncio
from asyncio import timeout
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from threading import Event
from typing import TYPE_CHECKING

from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.services.business.workspace_resources.records import (
    OwnedResourceRecord,
    ThreadRef,
)

if TYPE_CHECKING:
    from app.services.business.workspace_resources.owner import WorkspaceResourceOwner

OPERATION_CLOSE_WAIT_SECONDS = 30.0
OPERATION_ADMISSION_WAIT_SECONDS = 30.0


@asynccontextmanager
async def resource_operation_dispatch(
    owner: WorkspaceResourceOwner, record: OwnedResourceRecord, lease_id: str,
    *, operation: ResourceOperations | None,
) -> AsyncIterator[None]:
    """同一原lease覆盖派发、正式回执提交及失败写盘，不能靠finally猜测成功。"""
    # TODO(5.2)：版本管理及传输授权的派发仍须沿各自原入口迁齐，不能据此声明全部接管完成。
    cancelled = Event()

    def prepare() -> None:
        if not cancelled.is_set() and (operation is None or not operation._closed):
            owner._leases.mark_operation_dispatched(lease_id)

    preparing = asyncio.create_task(run_sync_shielded(prepare))

    async def settle_unstarted() -> None:
        await preparing
        lease = owner._leases.get_lease(lease_id)
        # 原调用尚未进入外部回调，可沿原账本撤销自己已完成的准备。
        if lease is not None and lease.status == "dispatching":
            await run_sync_shielded(owner._leases.finish_operation_dispatch, lease_id)

    try:
        await asyncio.shield(preparing)
    except asyncio.CancelledError as original:
        cancelled.set()

        try:
            await run_cleanup_shielded(settle_unstarted)
        except BaseException as failure:
            raise original from failure
        raise
    if operation is not None:
        try:
            operation._assert_open()
        except RuntimeError as original:
            # close可能在原写盘期间开始；未进入外部回调的准备不能继续派发。
            try:
                await run_cleanup_shielded(settle_unstarted)
            except BaseException as failure:
                raise original from failure
            raise
    try:
        yield
    except BaseException as error:
        await owner._unknown(record, error, lease_id=lease_id)
        raise
    else:
        confirming = asyncio.create_task(run_sync_shielded(owner._leases.finish_operation_dispatch, lease_id))
        try:
            await asyncio.shield(confirming)
        except asyncio.CancelledError as original:
            try:
                await run_cleanup_shielded(lambda: confirming)
            except BaseException as failure:
                raise original from failure
            raise


class ResourceOperations:
    def __init__(
        self, owner: WorkspaceResourceOwner, thread: ThreadRef,
        holder_id: str, operation_id: str, created_by_turn_id: str | None,
    ) -> None:
        if not holder_id or not operation_id:
            raise ValueError("外部资源 operation 缺少真实 holder 或 operation identity")
        self._owner = owner
        self._thread = thread
        self._holder_id = holder_id
        self._operation_id = operation_id
        self._created_by_turn_id = created_by_turn_id
        self._lease_refs: dict[str, str] = {}
        self._pending_resources: set[str] = set()
        self._closed = False

    @asynccontextmanager
    async def _resource(self, resource_id: str) -> AsyncIterator[OwnedResourceRecord]:
        self._assert_open()
        # 只限制本次资源准入；锁继续覆盖真实调用，不能把准入到期当作外部终止。
        # TODO(5.4)：其它同步 writer、不合作取消与整个工具operation总期限仍须原owner核实。
        async with AsyncExitStack() as access:
            budget = timeout(OPERATION_ADMISSION_WAIT_SECONDS)
            try:
                async with budget:
                    await self._owner.recover((resource_id,))
                    await access.enter_async_context(self._owner._resource_lock(resource_id))
                    self._assert_open()
                    record = self._owner._require_owned_resource_record(resource_id)
                    self._owner._verified(record)
                    if self._thread not in record.thread_associations:
                        raise ValueError(f"外部资源 不属于当前精确 Thread 关联: {resource_id}")
                    if resource_id not in self._lease_refs:
                        await self._acquire_lease(record, creating=False)
                        self._assert_open()
            except TimeoutError as error:
                if not budget.expired():
                    raise
                raise TimeoutError(
                    "外部资源 operation 准入等待超过 30 秒: "
                    f"operation_id={self._operation_id}, holder_id={self._holder_id}, resource_id={resource_id}"
                ) from error
            yield record

    async def _acquire_lease(self, record: OwnedResourceRecord, *, creating: bool) -> None:
        cancelled = Event()

        def acquire() -> None:
            # 排队时取消或关闭不再开始新写入；开始后的原生调用必须真实结算。
            if cancelled.is_set() or self._closed:
                return
            if creating:
                self._owner._leases.register_external(
                    resource_id=record.resource_id, kind=self._owner.lease_kind,
                    lifetime_scope=record.retention_scope, created_by_turn_id=self._created_by_turn_id,
                )
                if cancelled.is_set() or self._closed:
                    return
            self._lease_refs[record.resource_id] = self._owner._acquire(
                record, self._thread, self._holder_id, self._operation_id,
            )

        acquiring = asyncio.create_task(run_sync_shielded(acquire))
        already_pending = record.resource_id in self._pending_resources
        self._pending_resources.add(record.resource_id)
        try:
            await asyncio.shield(acquiring)
        except asyncio.CancelledError as original:
            cancelled.set()
            try:
                await run_cleanup_shielded(lambda: acquiring)
            except BaseException as failure:  # 保留原取消及迟到写盘失败。
                raise original from failure
            raise
        finally:
            if not already_pending:
                self._pending_resources.remove(record.resource_id)

    async def _release(self, resource_id: str, lock: asyncio.Lock) -> None:
        # 首条intent尚未发布时也等待创建者登记的原锁，不要求已有业务记录。
        async with lock:
            lease_id = self._lease_refs.get(resource_id)
            if lease_id is not None:
                def release() -> None:
                    self._owner._release_operation_lease(lease_id)
                    # 原账本持久释放成功后才遗忘引用，失败沿同一 operation 重试。
                    del self._lease_refs[resource_id]

                await run_sync_shielded(release)

    async def close(self) -> None:
        self._closed = True
        # 全批只取一次预算；取消只排空本次锁等待，原引用保留供同operation重试。
        # TODO(5.4)：worker 排队及写盘须结算，预算不能抢占原生 I/O；外部终态仍须 owner 核实。
        budget = asyncio.timeout(OPERATION_CLOSE_WAIT_SECONDS)
        resources = dict.fromkeys((*self._lease_refs, *sorted(self._pending_resources)))
        # 在让出事件循环前冻结原锁；未发布intent失败后会删除其锁登记。
        tasks = [asyncio.create_task(self._release(resource_id, self._owner._resource_locks[resource_id]))
                 for resource_id in resources]
        batch = asyncio.gather(*tasks, return_exceptions=True)

        async def settle() -> None:
            await batch

        try:
            async with budget:
                try:
                    results = await asyncio.shield(batch)
                except asyncio.CancelledError:
                    # 先取得原取消，再排空本批；迟到写盘失败不能让 gather 丢失原取消消息。
                    for task in tasks:
                        task.cancel()
                    await run_cleanup_shielded(settle)
                    raise
            errors = [result for result in results if isinstance(result, BaseException)]
        except (TimeoutError, asyncio.CancelledError) as failure:
            if isinstance(failure, TimeoutError) and not budget.expired():
                raise
            # gather已排空取消；到期或调用方取消都保留此前返回的原错误。
            errors = [error for task in tasks if not task.cancelled() and (error := task.exception()) is not None]
            if isinstance(failure, asyncio.CancelledError):
                if errors:
                    raise failure from BaseExceptionGroup("外部资源 operation 关闭失败", errors)
                raise
            errors.append(TimeoutError(
                "外部资源 operation 关闭等待已达总截止时间；保留未释放占用: "
                f"operation_id={self._operation_id}, holder_id={self._holder_id}, "
                f"resource_ids={','.join(self._lease_refs)}"
            ))
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("外部资源 operation 关闭失败", errors)

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("外部资源 operation 已关闭，拒绝迟到副作用")
