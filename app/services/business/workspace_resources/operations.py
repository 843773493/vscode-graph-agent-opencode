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
        # 各资源分别等待自己的锁；单项挂起或写盘失败不阻断其它占用释放。
        results = await asyncio.gather(
            *(self._release(resource_id) for resource_id in tuple(self._lease_refs)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("外部资源 operation 关闭失败", errors)

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("外部资源 operation 已关闭，拒绝迟到副作用")
