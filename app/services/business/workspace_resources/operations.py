"""单次受信调用的 lease 引用；不创建、操作或停止外部实例。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.services.business.workspace_resources.records import (
    OwnedResourceRecord,
    ThreadRef,
)

if TYPE_CHECKING:
    from app.services.business.workspace_resources.owner import WorkspaceResourceOwner

OPERATION_CLOSE_WAIT_SECONDS = 30.0


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
        self._closed = False

    @asynccontextmanager
    async def _resource(self, resource_id: str) -> AsyncIterator[OwnedResourceRecord]:
        self._assert_open()
        await self._owner.recover((resource_id,))
        async with self._owner._resource_lock(resource_id):
            self._assert_open()
            record = self._owner._require_owned_resource_record(resource_id)
            self._owner._verified(record)
            if self._thread not in record.thread_associations:
                raise ValueError(f"外部资源 不属于当前精确 Thread 关联: {resource_id}")
            if resource_id not in self._lease_refs:
                self._lease_refs[resource_id] = self._owner._acquire(
                    record, self._thread, self._holder_id, self._operation_id,
                )
            yield record

    async def _release(self, resource_id: str) -> None:
        async with self._owner._resource_lock(resource_id):
            lease_id = self._lease_refs.get(resource_id)
            if lease_id is not None:
                self._owner._release_operation_lease(lease_id)
                # 原账本持久释放成功后才遗忘引用，失败沿同一 operation 重试。
                del self._lease_refs[resource_id]

    async def close(self) -> None:
        self._closed = True
        # 全批只取一次预算；取消只排空本次锁等待，原引用保留供同operation重试。
        # TODO(5.4)：同步账本写盘与不合作取消不能由此预算抢占，外部终态仍须owner核实。
        budget = asyncio.timeout(OPERATION_CLOSE_WAIT_SECONDS)
        tasks = [asyncio.create_task(self._release(resource_id)) for resource_id in tuple(self._lease_refs)]
        try:
            async with budget:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
        except TimeoutError:
            if not budget.expired():
                raise
            # gather已排空取消；保留到期前返回的原错误，不被整批期限覆盖。
            errors = [error for task in tasks if not task.cancelled() and (error := task.exception()) is not None]
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
