"""单次受信调用的 lease 引用；不创建、操作或停止外部实例。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.business.workspace_resources.records import ThreadRef

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

    def _use(self, resource_id: str) -> None:
        self._assert_open()
        record = self._owner._require(resource_id)
        self._owner._verified(record)
        if self._thread not in record.thread_associations:
            raise ValueError(f"外部资源 不属于当前精确 Thread 关联: {resource_id}")
        if resource_id not in self._lease_refs:
            self._lease_refs[resource_id] = self._owner._acquire(
                record, self._thread, self._holder_id, self._operation_id,
            )

    def _release_locked(self, resource_id: str) -> None:
        lease_id = self._lease_refs.pop(resource_id, None)
        if lease_id is not None:
            self._owner._leases.release(lease_id)

    async def close(self) -> None:
        async with self._owner._lock:
            for resource_id in tuple(self._lease_refs):
                self._release_locked(resource_id)
            self._closed = True

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("外部资源 operation 已关闭，拒绝迟到副作用")
