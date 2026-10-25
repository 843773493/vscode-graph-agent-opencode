"""一次性冻结恢复版本，按原资源锁同步；不保存第二套资源或 operation 事实。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.services.business.workspace_resources.facts import (
    index_resources,
    lease_status,
    observe_resource,
)

if TYPE_CHECKING:
    from app.services.business.workspace_resources.owner import WorkspaceResourceOwner


class ResourceRecovery:
    def __init__(self, owner: WorkspaceResourceOwner) -> None:
        self._owner = owner
        self._prepare_lock = asyncio.Lock()
        # 只协调本进程尚未同步的冻结版本；持久资源与占用仍分别由原 owner/lease 账本保存。
        self._pending: dict[str, int | None] | None = None

    async def recover(self, resource_ids: Iterable[str] | None = None) -> None:
        await self._prepare()
        assert self._pending is not None
        selected = tuple(self._pending) if resource_ids is None else tuple(dict.fromkeys(resource_ids))
        tasks = [asyncio.create_task(self._recover_resource(resource_id)) for resource_id in selected]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            # 调用取消必须排空自己创建的等待者，不能留下脱离调用生命周期的恢复任务。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        errors = [result for result in results if result is not None]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("外部资源 恢复存在未核实资源", errors)

    async def _prepare(self) -> None:
        if self._pending is not None:
            return
        # TODO：首次清单请求、锁等待和所有外部调用仍须统一到原 operation 的总期限。
        async with self._prepare_lock:
            if self._pending is not None:
                return
            owner = self._owner
            unknown_results = {
                record.resource_id for record in owner._store.records.values()
                if record.reconciliation_status == "unknown"
            }
            observed = index_resources(await owner._transport.list_resources(), owner.workspace_id, owner._store.records, owner.kind)
            statuses: dict[str, str] = {}
            for record in owner._store.records.values():
                owner._leases.register_external(
                    resource_id=record.resource_id,
                    kind=owner.lease_kind,
                    lifetime_scope=record.retention_scope,
                )
                runtime = observed.get(record.resource_id)
                observe_resource(record, runtime, owner.kind)
                statuses[record.resource_id] = lease_status(record)
            # 每个领域只对账自身 lease，不修改另一 owner 的运行事实。
            owner._leases.reconcile(statuses, kind=owner.lease_kind)
            for record in owner._store.records.values():
                owner._release_committed_fork_leases(record)
                owner._reconcile_stopped_record(record, observed.get(record.resource_id))
                if owner._leases.leases_for_resource(record.resource_id):
                    record.reconciliation_status = "unknown" if record.resource_id in unknown_results else "reconcile_required"
                    record.error = record.error or "前次 operation 结果尚未核实；未自动重放或结清 lease"
                runtime = observed.get(record.resource_id)
                if runtime is not None:
                    # Python 重启使旧传输授权失效；先落盘新版本，再要求 Node 撤销旧连接。
                    record.owner_revision = (
                        max(
                            record.owner_revision,
                            int(runtime["transport_owner_revision"]),
                        )
                        + 1
                    )
            if owner._store.records:
                owner._store.persist()
            self._pending = {
                record.resource_id: record.owner_revision if record.resource_id in observed else None
                for record in owner._store.records.values()
            }

    async def _recover_resource(self, resource_id: str) -> Exception | None:
        owner = self._owner
        async with owner._resource_lock(resource_id):
            assert self._pending is not None
            if resource_id not in self._pending:
                return None
            record = owner._require(resource_id)
            revision = self._pending[resource_id]
            failure = None
            try:
                if revision is not None:
                    await owner._transport.sync_owner_revision(resource_id, revision)
            except BaseException as error:
                owner._unknown(record, error)
                if not isinstance(error, Exception):
                    raise
                failure = error
            owner._sync_ledger(record)
            del self._pending[resource_id]
            return failure
