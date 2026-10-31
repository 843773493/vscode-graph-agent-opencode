"""一次性冻结恢复版本，按原资源锁同步；不保存第二套资源或 operation 事实。"""

from __future__ import annotations

import asyncio
from asyncio import timeout
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from app.abstractions.workspace_resources import ResourceSnapshot
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.business.workspace_resources.facts import (
    index_resources,
    lease_status,
    observe_resource,
)

if TYPE_CHECKING:
    from app.services.business.workspace_resources.owner import WorkspaceResourceOwner


@dataclass
class RecoveryVersion:
    revision: int | None
    # 普通失败后仍可查询unknown；只有显式停止继续核实原冻结版本。
    management_only: bool = False


class ResourceRecovery:
    def __init__(self, owner: WorkspaceResourceOwner) -> None:
        self._owner = owner
        # 只协调本进程尚未同步的冻结版本；持久资源与占用仍分别由原 owner/lease 账本保存。
        self._pending: dict[str, RecoveryVersion] | None = None
        self._prepare_lock = asyncio.Lock()

    def retain_revision(self, resource_id: str, revision: int) -> None:
        """本地已提交而外部同步未核实，沿原冻结版本等待显式管理重试。"""
        assert self._pending is not None
        self._pending[resource_id] = RecoveryVersion(revision, management_only=True)

    async def recover(self, resource_ids: Iterable[str] | None = None) -> None:
        # 一个调用只取得一次预算，清单、准备锁和各资源同步共同消耗。
        deadline = timeout(30)
        errors: list[Exception] = []
        try:
            async with deadline:
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
                    settled = await asyncio.gather(*tasks, return_exceptions=True)
                    # 已完成的同步返回原异常对象；取消其它等待不能将这些失败丢掉。
                    errors = [result for result in settled if isinstance(result, Exception)]
                    raise
                errors = [result for result in results if result is not None]
                if len(errors) == 1:
                    raise errors[0]
                if errors:
                    raise ExceptionGroup("外部资源 恢复存在未核实资源", errors)
        except asyncio.CancelledError as error:
            if errors:
                raise error from ExceptionGroup("外部资源 恢复存在未核实资源", errors)
            raise
        except TimeoutError as error:
            if not deadline.expired():
                raise
            expired = TimeoutError(
                f"外部资源冷恢复等待超过 30 秒: workspace_id={self._owner.workspace_id} "
                f"kind={self._owner.kind}；保留未核实版本与占用，未确认外部操作停止"
            )
            if errors:
                raise ExceptionGroup("外部资源 恢复存在未核实资源", [*errors, expired]) from error
            raise expired from error

    async def _prepare(self) -> None:
        if self._pending is not None:
            return
        owner = self._owner
        # 清单读取不持共享锁；一个回执挂起不能阻断其它调用自行核实同一 owner。
        items = await owner._transport.list_resources()
        # 网络不占准备锁；首个成功清单持有完整写盘与版本冻结，取消不能留下半次准备。
        async with self._prepare_lock:
            if self._pending is not None:
                return
            preparing = asyncio.create_task(self._prepare_records(items))
            try:
                await asyncio.shield(preparing)
            finally:
                await run_cleanup_shielded(lambda: preparing)

    async def _prepare_records(self, items: list[ResourceSnapshot]) -> None:
        owner = self._owner
        observed = index_resources(items, owner.workspace_id, owner._store.ids(), owner.kind)
        records = owner._store.all()
        unknown_results = {
            record.resource_id for record in records
            if record.reconciliation_status == "unknown"
        }
        statuses: dict[str, str] = {}
        for record in records:
            owner._leases.register_external(
                resource_id=record.resource_id,
                kind=owner.lease_kind,
                lifetime_scope=record.retention_scope,
            )
            runtime = observed.get(record.resource_id)
            observe_resource(record, runtime, owner.kind)
            statuses[record.resource_id] = lease_status(record)
        await owner._store.save(*records)
        # 每个领域只对账自身 lease，不修改另一 owner 的运行事实。
        owner._leases.reconcile(statuses, kind=owner.lease_kind)
        for record in records:
            owner._release_committed_fork_leases(record)
        pending_by_resource = owner._leases.leases_for_resources(owner._store.ids())
        for record in records:
            outstanding_leases = await owner._reconcile_stopped_record(
                record, observed.get(record.resource_id),
                pending_leases=pending_by_resource[record.resource_id],
            )
            if outstanding_leases:
                record.reconciliation_status = "unknown" if record.resource_id in unknown_results else "reconcile_required"
                record.error = record.error or "前次 operation 结果尚未核实；未自动重放或结清 lease"
            runtime = observed.get(record.resource_id)
            if runtime is not None:
                # Python 重启使旧传输授权失效；先落盘新版本，再要求 Node 撤销旧连接。
                record.owner_revision = (
                    max(
                        record.owner_revision,
                        cast(int, runtime["transport_owner_revision"]),
                    )
                    + 1
                )
        if owner._store.ids():
            await owner._store.save(*records)
        self._pending = {
            record.resource_id: RecoveryVersion(record.owner_revision if record.resource_id in observed else None)
            for record in records
        }

    async def _recover_resource(self, resource_id: str) -> Exception | None:
        async with self._owner._resource_lock(resource_id):
            return await self.recover_locked(resource_id, for_stop=False)

    async def recover_locked(self, resource_id: str, *, for_stop: bool) -> Exception | None:
        """调用方持原资源锁；管理停止先核准入，再撤权而不等待依赖停止的旧执行。"""
        owner = self._owner
        assert self._pending is not None
        if resource_id not in self._pending:
            return None
        pending = self._pending[resource_id]
        if pending.management_only and not for_stop:
            return None
        record = owner._require_owned_resource_record(resource_id)
        try:
            if pending.revision is not None:
                await owner._transport.sync_owner_revision(resource_id, pending.revision, for_stop=for_stop)
        except BaseException as error:
            if isinstance(error, Exception):
                pending.management_only = True
            await owner._unknown(record, error)
            if not isinstance(error, Exception):
                raise
            return error
        owner._sync_ledger(owner._require_owned_resource_record(resource_id))
        del self._pending[resource_id]
        return None
