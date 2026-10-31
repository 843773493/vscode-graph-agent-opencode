"""创建intent、原operation派发与正式回执结算；资源事实仍由原owner持有。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.abstractions.workspace_resources import (
    ResourceRequestError,
    ResourceSnapshot,
    RetentionScope,
)
from app.core.failures import report_failure
from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.services.business.workspace_resources.facts import (
    observe_resource,
    resource_projection,
)
from app.services.business.workspace_resources.operations import (
    resource_operation_dispatch,
)
from app.services.business.workspace_resources.records import OwnedResourceRecord

if TYPE_CHECKING:
    from app.services.business.workspace_resources.operations import ResourceOperations
    from app.services.business.workspace_resources.owner import WorkspaceResourceOwner


async def create_resource(
    owner: WorkspaceResourceOwner, *, operation: ResourceOperations, retention_scope: RetentionScope, title: str,
    create: Callable[[OwnedResourceRecord], Awaitable[ResourceSnapshot]],
) -> ResourceSnapshot:
    operation._assert_open()
    ref = operation._thread
    stamp = owner._now()
    prefix = "term" if owner.kind == "terminal" else "browser"
    record = OwnedResourceRecord(
        resource_id=f"{prefix}_{create_uuid_hex()}",
        workspace_id=owner.workspace_id, source_thread=ref,
        retention_scope=retention_scope, title=title,
        created_at=stamp, updated_at=stamp, thread_associations=[ref],
    )
    lock = asyncio.Lock()
    owner._resource_locks[record.resource_id] = lock
    operation._pending_resources.add(record.resource_id)
    try:
        async with lock:
            callback_entered = False
            try:
                await owner._store.save(record)
                operation._assert_open()
                await operation._acquire_lease(record, creating=True)
                operation._assert_open()
                lease_id = operation._lease_refs[record.resource_id]
                async with resource_operation_dispatch(owner, record, lease_id, operation=operation):
                    callback_entered = True
                    runtime = await create(record)
                    observe_resource(record, runtime, owner.kind, creation_receipt=True)
                    owner._verified(record)
                    await owner._store.save(record)
                    await owner._sync_ledger(record)
                    projection = resource_projection(record, runtime, owner.kind)
                return projection
            except BaseException as error:
                lease_id = operation._lease_refs.get(record.resource_id)
                if callback_entered:
                    receipt = error.creation_receipt if isinstance(error, ResourceRequestError) else None
                    assert lease_id is not None
                    await _record_creation_failure(owner, record, receipt, lease_id, error)
                elif (saved := owner._store.get(record.resource_id)) is None:
                    del owner._resource_locks[record.resource_id]
                else:
                    # 只有仍在场的原调用能证明未进入回调，终态提交后才结清其原lease。
                    await run_cleanup_shielded(lambda: _close_unstarted_creation(
                        owner, saved, operation._created_by_turn_id, lease_id=lease_id,
                    ))
                raise
    finally:
        operation._pending_resources.remove(record.resource_id)


async def _close_unstarted_creation(
    owner: WorkspaceResourceOwner, record: OwnedResourceRecord, created_by_turn_id: str | None, *, lease_id: str | None,
) -> None:
    """只关闭原调用确认从未进入外部创建回调的 intent，不补造 operation lease。"""
    # TODO(5.2)：原调用已随 Backend 退出的旧 intent 仍须接管；清单缺失不能证明未创建。
    await run_sync_shielded(owner._leases.register_external,
        resource_id=record.resource_id, kind=owner.lease_kind,
        lifetime_scope=record.retention_scope, created_by_turn_id=created_by_turn_id,
    )
    record.status = "deleted"
    record.thread_associations.clear()
    record.reconciliation_status = "verified"
    record.error = None
    record.updated_at = owner._now()
    await owner._store.save(record)
    await owner._sync_ledger(record)
    if lease_id is not None:
        await owner._leases.settle(lease_id)


async def _record_creation_failure(
    owner: WorkspaceResourceOwner, record: OwnedResourceRecord, receipt: object, lease_id: str, error: BaseException,
) -> bool:
    if receipt is None:
        return False
    record = owner._require_owned_resource_record(record.resource_id)
    # 正式拒绝必须绑定本次创建 intent；HTTP 状态或任意资源快照都不能替代它。
    if not isinstance(receipt, dict) or type(receipt.get("owner_revision")) is not int or (
        receipt.get("resource_id"), receipt.get("workspace_id"), receipt.get("owner_revision")
    ) != (record.resource_id, record.workspace_id, record.owner_revision):
        report_failure(error, detail=f"外部资源创建失败回执身份冲突: resource_id={record.resource_id}")
        return False
    outcome = receipt.get("outcome")
    if outcome == "not_created" and receipt.get("resource") is None:
        record.status = "deleted"
        record.thread_associations.clear()
    elif outcome == "failed":
        runtime = receipt.get("resource")
        expected_status = "failed" if owner.kind == "browser" else "terminated"
        if not isinstance(runtime, dict) or type(runtime.get("transport_owner_revision")) is not int or (
            runtime.get(f"{owner.kind}_id"), runtime.get("workspace_id"),
            runtime.get("transport_owner_revision"), runtime.get("status"),
            runtime.get("reconciliation_status"),
        ) != (record.resource_id, record.workspace_id, record.owner_revision, expected_status, "verified"):
            report_failure(error, detail=f"外部资源创建失败回执未核实: resource_id={record.resource_id}")
            return False
        observe_resource(record, runtime, owner.kind)
    else:
        report_failure(error, detail=f"外部资源创建失败回执无效: resource_id={record.resource_id}")
        return False
    record.reconciliation_status = "verified"
    record.error = None
    record.updated_at = owner._now()
    await owner._store.save(record)
    await owner._leases.settle(lease_id)
    await owner._sync_ledger(record)
    return True
