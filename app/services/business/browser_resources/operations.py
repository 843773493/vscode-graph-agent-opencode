"""一次受信 Browser 调用从创建到最终回执持有同一 operation lease。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.abstractions.browser_resources import BrowserAction, BrowserRequestError
from app.abstractions.workspace_resources import ResourceSnapshot, RetentionScope
from app.services.business.workspace_resources.facts import (
    observe_resource,
    resource_projection,
)
from app.services.business.workspace_resources.operations import ResourceOperations
from app.services.business.workspace_resources.records import OwnedResourceRecord

if TYPE_CHECKING:
    from app.services.business.browser_resources.owner import BrowserResourceOwner


class BrowserOperations(ResourceOperations):
    _owner: BrowserResourceOwner

    async def create_browser(
        self, *, retention_scope: RetentionScope = "thread", title: str = "Agent browser",
        url: str, viewport: dict[str, int] | None = None,
        device_profile: str | None = None, device_orientation: str | None = None,
    ) -> ResourceSnapshot:
        self._assert_open()
        if retention_scope not in {"thread", "workspace"}:
            raise ValueError(f"Browser 保留范围无效: {retention_scope}")
        return await self._owner._create(
            ref=self._thread, retention_scope=retention_scope, title=title, url=url,
            viewport=viewport, device_profile=device_profile, device_orientation=device_orientation,
            holder_id=self._holder_id, operation_id=self._operation_id,
            created_by_turn_id=self._created_by_turn_id, lease_refs=self._lease_refs,
        )

    async def list_resources(self) -> list[ResourceSnapshot]:
        self._assert_open()
        return await self._owner.list_resources(
            session_id=self._thread.session_id, thread_id=self._thread.thread_id,
        )

    async def perform(
        self, browser_id: str, action: BrowserAction, payload: ResourceSnapshot | None = None,
    ) -> ResourceSnapshot:
        async with self._resource(browser_id) as record:
            return await self.call_locked(
                self._owner, record, action, payload or {},
                actor="agent", lease_id=self._lease_refs[browser_id],
            )

    async def promote(self, browser_id: str) -> ResourceSnapshot:
        async with self._resource(browser_id) as record:
            return await self._owner._command_record(
                record, "promote", record.owner_revision, self._thread,
                self._holder_id, self._operation_id, self._lease_refs[browser_id],
            )

    @staticmethod
    async def call_locked(
        owner: BrowserResourceOwner, record: OwnedResourceRecord,
        action: BrowserAction, payload: ResourceSnapshot, *, actor: str, lease_id: str,
    ) -> ResourceSnapshot:
        failure: BrowserRequestError | None = None
        try:
            try:
                result = await owner._transport.perform(record.resource_id, action, payload, actor=actor)
            except BrowserRequestError as error:
                receipt = error.payload
                if not isinstance(receipt, dict) or receipt.get("operation_outcome") != "failed":
                    raise
                failure = error
                result = receipt
            # 成功与正式失败共用核验和提交；失败回执的校验/写盘错误也必须保护原占用。
            runtime = result.get("browser")
            if not isinstance(runtime, dict):
                raise TypeError("Browser operation 缺少 Playwright 完整事实")
            if type(runtime.get("transport_owner_revision")) is not int or runtime["transport_owner_revision"] != record.owner_revision:
                raise ValueError("Browser operation 回执版本与本次操作不一致")
            observe_resource(record, runtime, owner.kind)
            if failure is not None and record.reconciliation_status != "verified":
                # 正式失败仍可能报告未核实的页面转换；保留其原HTTP错误和待核实占用。
                raise failure
            owner._verified(record)
            owner._store.save(record)
            owner._sync_ledger(record)
            projected = {**result, "browser": resource_projection(record, runtime, owner.kind)}
        except BaseException as error:
            owner._unknown(record, error, lease_id=lease_id)
            if failure is not None and error is not failure:
                raise error from failure
            raise
        if failure is not None:
            raise failure
        return projected
