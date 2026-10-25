"""Browser 领域 owner：来源、关联及工具执行统一由 Python 核定。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.abstractions.browser_resources import BrowserAction, BrowserTransport
from app.abstractions.workspace_resources import ResourceSnapshot, RetentionScope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.business.browser_resources.operations import BrowserOperations
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
        self, *, ref: ThreadRef, retention_scope: RetentionScope, title: str, url: str,
        viewport: dict[str, int] | None, device_profile: str | None,
        device_orientation: str | None, holder_id: str, operation_id: str,
        created_by_turn_id: str | None, lease_refs: dict[str, str],
    ) -> ResourceSnapshot:
        return await self._create_record(
            ref=ref, retention_scope=retention_scope, title=title,
            holder_id=holder_id, operation_id=operation_id,
            created_by_turn_id=created_by_turn_id, lease_refs=lease_refs,
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
        async with self.admit_thread(session_id, thread_id) as ref:
            operations = BrowserOperations(self, ref, holder_id, operation_id, created_by_turn_id)
            try:
                yield operations
            finally:
                await run_cleanup_shielded(operations.close)

    async def perform(
        self, resource_id: str, *, action: BrowserAction, payload: ResourceSnapshot,
        expected_revision: int, holder_id: str, operation_id: str,
        thread: ThreadRef | None = None,
    ) -> ResourceSnapshot:
        await self.recover((resource_id,))
        if thread is not None:
            async with self.admit_thread(thread.session_id, thread.thread_id):
                return await self._perform(resource_id, action, payload, expected_revision,
                                           holder_id, operation_id, thread)
        return await self._perform(resource_id, action, payload, expected_revision,
                                   holder_id, operation_id, None)

    async def _perform(
        self, resource_id: str, action: BrowserAction, payload: ResourceSnapshot,
        revision: int, holder_id: str, operation_id: str, thread: ThreadRef | None,
    ) -> ResourceSnapshot:
        async with self._resource_lock(resource_id):
            record = self._require(resource_id)
            self._verified(record)
            if record.owner_revision != revision:
                raise ResourceConflict("Browser 操作版本过期，请刷新资源")
            if thread is None and record.retention_scope != "workspace":
                raise ResourceConflict("临时 Browser 操作必须绑定有效关联 Thread")
            if thread is not None and thread not in record.thread_associations:
                raise ResourceConflict("Thread 尚未关联 Browser")
            with self._management_lease(record, thread, holder_id, operation_id) as lease_id:
                return await BrowserOperations.call_locked(self, record, action, payload, actor="user:http", lease_id=lease_id)
