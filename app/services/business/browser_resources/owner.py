"""Browser 领域 owner：来源、关联及工具执行统一由 Python 核定。"""

from __future__ import annotations

from asyncio import timeout
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from app.abstractions.browser_resources import BrowserAction, BrowserTransport
from app.abstractions.workspace_resources import ResourceSnapshot, RetentionScope
from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.workspace_resources.creation import create_resource
from app.services.business.workspace_resources.operations import (
    OPERATION_ADMISSION_WAIT_SECONDS,
)
from app.services.business.workspace_resources.owner import (
    ResourceConflict,
    WorkspaceResourceOwner,
)
from app.services.business.workspace_resources.records import ThreadRef


class BrowserResourceOwner(WorkspaceResourceOwner):
    kind = "browser"
    lease_kind = "browser_context"
    _transport: BrowserTransport

    async def create_browser(
        self, *, session_id: str, holder_id: str, operation_id: str,
        thread_id: str | None = None, retention_scope: RetentionScope = "thread",
        title: str = "Browser", url: str = "about:blank",
        viewport: dict[str, int] | None = None, device_profile: str | None = None,
        device_orientation: str | None = None, created_by_turn_id: str | None = None,
    ) -> ResourceSnapshot:
        async with self.operation(
            session_id=session_id, thread_id=thread_id, holder_id=holder_id,
            operation_id=operation_id, created_by_turn_id=created_by_turn_id,
        ) as operation:
            return await operation.create_browser(
                retention_scope=retention_scope, title=title, url=url, viewport=viewport,
                device_profile=device_profile, device_orientation=device_orientation,
            )

    async def _create(
        self, *, operation: BrowserOperations, retention_scope: RetentionScope, title: str, url: str,
        viewport: dict[str, int] | None, device_profile: str | None,
        device_orientation: str | None,
    ) -> ResourceSnapshot:
        return await create_resource(
            self, operation=operation, retention_scope=retention_scope, title=title,
            create=lambda record: self._transport.create_browser(
                browser_id=record.resource_id, owner_revision=record.owner_revision,
                title=title, url=url, viewport=viewport, device_profile=device_profile,
                device_orientation=device_orientation,
            ),
        )

    @asynccontextmanager
    async def operation(
        self, *, session_id: str, thread_id: str | None, holder_id: str,
        operation_id: str, created_by_turn_id: str | None = None,
    ) -> AsyncIterator[BrowserOperations]:
        await self.recover(())
        async with (
            self.admit_thread(session_id, thread_id) as ref,
            BrowserOperations(self, ref, holder_id, operation_id, created_by_turn_id) as operations,
        ):
            yield operations

    async def perform(
        self, resource_id: str, *, action: BrowserAction, payload: ResourceSnapshot,
        expected_revision: int, holder_id: str, operation_id: str,
        thread: ThreadRef | None = None,
    ) -> ResourceSnapshot:
        # 准入共用一次预算；真实动作仍使用原传输期限，包括 run 的显式 timeoutMs。
        # TODO(5.4)：原生写盘和不合作取消须结算，准入预算不保证整个动作可强制终止。
        async with AsyncExitStack() as access:
            budget = timeout(OPERATION_ADMISSION_WAIT_SECONDS)
            try:
                async with budget:
                    await self.recover((resource_id,))
                    if thread is not None:
                        await access.enter_async_context(self.admit_thread(thread.session_id, thread.thread_id))
                    await access.enter_async_context(self._resource_lock(resource_id))
                    record = self._require_owned_resource_record(resource_id)
                    self._verified(record)
                    if record.owner_revision != expected_revision:
                        raise ResourceConflict("Browser 操作版本过期，请刷新资源")
                    if thread is None and record.retention_scope != "workspace":
                        raise ResourceConflict("临时 Browser 操作必须绑定有效关联 Thread")
                    if thread is not None and thread not in record.thread_associations:
                        raise ResourceConflict("Thread 尚未关联 Browser")
                    lease_id = await access.enter_async_context(
                        self._management_lease(record, thread, holder_id, operation_id),
                    )
            except TimeoutError as error:
                if not budget.expired():
                    raise
                raise TimeoutError(
                    "Browser 动作准入等待超过 30 秒: "
                    f"resource_id={resource_id}, action={action}, "
                    f"holder_id={holder_id}, operation_id={operation_id}；未派发本次动作"
                ) from error
            return await BrowserOperations.call_locked(
                self, record, action, payload, actor="user:http", lease_id=lease_id, operation=None,
            )
