"""两个领域 owner 共用的真实保留、关联、lease 与释放规则。"""

from __future__ import annotations

import asyncio
import logging
from asyncio import timeout
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.workspace_resources import (
    ResourceKind,
    ResourceRequestError,
    ResourceSessionReader,
    ResourceSnapshot,
    ResourceThreadResolver,
    ResourceTransport,
    ResourceTransportGrantRejected,
    RetentionScope,
)
from app.core.identifier import create_uuid_hex
from app.core.phase_observer import observe_phase
from app.services.business.workspace_resources.facts import (
    ORPHAN_GRACE_SECONDS,
    STOPPED_STATUSES,
    index_resources,
    lease_status,
    observe_resource,
    public_error,
    resource_projection,
    resource_target_facts,
)
from app.services.business.workspace_resources.records import (
    ForkAttachReceipt,
    OwnedResourceRecord,
    ResourceRecordStore,
    ThreadRef,
)
from app.services.business.workspace_resources.recovery import ResourceRecovery
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
    ResourceLease,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetContext,
)

logger = logging.getLogger(__name__)

class ResourceConflict(ValueError):
    """版本、关联或 operation lease 阻断；没有修改资源事实。"""

class WorkspaceResourceOwner:
    kind: ResourceKind
    lease_kind: str

    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_id: str,
        sessions: ResourceSessionReader,
        threads: ResourceThreadResolver,
        transport: ResourceTransport,
        leases: ExternalResourceLeaseLedger,
        thread_release_fence: Callable[[str, str, int], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if sessions.workspace_id != workspace_id:
            raise ValueError("外部资源 owner 与 Session owner 工作区不一致")
        self.workspace_id = workspace_id
        self.workspace_root = workspace_root.resolve()
        self._sessions = sessions
        self._threads = threads
        self._transport = transport
        self._leases = leases
        self._clock = clock
        self._thread_release_fence = thread_release_fence
        self._store = ResourceRecordStore(
            workspace_root=workspace_root, workspace_id=workspace_id, kind=self.kind
        )
        self._resource_locks = {resource_id: asyncio.Lock() for resource_id in self._store.ids()}
        self._recovery = ResourceRecovery(self)

    @asynccontextmanager
    async def admit_thread(
        self,
        session_id: str,
        thread_id: str | None,
    ) -> AsyncIterator[ThreadRef]:
        """来源只能由权威 catalog 核定；省略 Thread 仅表示后端解析 canonical main。"""
        async with self._sessions.session_write_admission(session_id):
            session = await self._sessions.get(session_id)
            if session.workspace_id != self.workspace_id:
                raise ValueError("外部资源 来源 Session 不属于当前工作区")
            ref = ThreadRef(
                session_id, thread_id or self._threads.main_thread_id(session_id)
            )
            self._threads.resolve_thread_node(ref.session_id, ref.thread_id)
            yield ref

    async def recover(self, resource_ids: Iterable[str] | None = None) -> None:
        await self._recovery.recover(resource_ids)

    async def _create_record(
        self, *, ref: ThreadRef, retention_scope: RetentionScope, title: str,
        holder_id: str, operation_id: str, created_by_turn_id: str | None,
        lease_refs: dict[str, str],
        create: Callable[[OwnedResourceRecord], Awaitable[ResourceSnapshot]],
    ) -> ResourceSnapshot:
        stamp = self._now()
        prefix = "term" if self.kind == "terminal" else "browser"
        record = OwnedResourceRecord(
            resource_id=f"{prefix}_{create_uuid_hex()}",
            workspace_id=self.workspace_id, source_thread=ref,
            retention_scope=retention_scope, title=title,
            created_at=stamp, updated_at=stamp, thread_associations=[ref],
        )
        lock = asyncio.Lock()
        async with lock:
            self._store.save(record)
            self._resource_locks[record.resource_id] = lock
            self._leases.register_external(
                resource_id=record.resource_id, kind=self.lease_kind,
                lifetime_scope=retention_scope, created_by_turn_id=created_by_turn_id,
            )
            lease_refs[record.resource_id] = self._acquire(record, ref, holder_id, operation_id)
            try:
                runtime = await create(record)
                observe_resource(record, runtime, self.kind)
                self._verified(record)
                self._store.save(record)
                self._sync_ledger(record)
                return resource_projection(record, runtime, self.kind)
            except BaseException as error:
                lease_id = lease_refs[record.resource_id]
                receipt = error.creation_receipt if isinstance(error, ResourceRequestError) else None
                self._unknown(record, error, lease_id=lease_id)
                self._record_creation_failure(record, receipt, lease_id)
                raise

    def _record_creation_failure(
        self, record: OwnedResourceRecord, receipt: object, lease_id: str,
    ) -> bool:
        if receipt is None:
            return False
        record = self._require_owned_resource_record(record.resource_id)
        # 正式拒绝必须绑定本次创建 intent；HTTP 状态或任意资源快照都不能替代它。
        if not isinstance(receipt, dict) or type(receipt.get("owner_revision")) is not int or (
            receipt.get("resource_id"), receipt.get("workspace_id"), receipt.get("owner_revision")
        ) != (record.resource_id, record.workspace_id, record.owner_revision):
            logger.error("外部资源创建失败回执身份冲突: resource_id=%s", record.resource_id)
            return False
        outcome = receipt.get("outcome")
        if outcome == "not_created" and receipt.get("resource") is None:
            record.status = "deleted"
            record.thread_associations.clear()
        elif outcome == "failed":
            runtime = receipt.get("resource")
            expected_status = "failed" if self.kind == "browser" else "terminated"
            if not isinstance(runtime, dict) or type(runtime.get("transport_owner_revision")) is not int or (
                runtime.get(f"{self.kind}_id"), runtime.get("workspace_id"),
                runtime.get("transport_owner_revision"), runtime.get("status"),
                runtime.get("reconciliation_status"),
            ) != (record.resource_id, record.workspace_id, record.owner_revision, expected_status, "verified"):
                logger.error("外部资源创建失败回执未核实: resource_id=%s", record.resource_id)
                return False
            observe_resource(record, runtime, self.kind)
        else:
            logger.error("外部资源创建失败回执无效: resource_id=%s", record.resource_id)
            return False
        record.reconciliation_status = "verified"
        record.error = None
        record.updated_at = self._now()
        self._store.save(record)
        self._leases.settle(lease_id)
        self._sync_ledger(record)
        return True

    async def list_resources(
        self,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        retention_scope: RetentionScope | None = None,
        source_session_id: str | None = None,
        source_thread_id: str | None = None,
    ) -> list[ResourceSnapshot]:
        # 一次领域列表的恢复、清单和各资源锁共同消耗入口预算。
        # TODO(5.4)：同步存储与不合作取消仍须由原执行owner核实收敛。
        deadline = timeout(30)
        try:
            async with deadline:
                await self.recover(())
                def matches(record: OwnedResourceRecord) -> bool:
                    refs = record.thread_associations
                    if record.retention_scope == "thread" and not refs:
                        refs = [record.source_thread]
                    return (
                        (retention_scope is None or record.retention_scope == retention_scope)
                        and (source_session_id is None or record.source_thread.session_id == source_session_id)
                        and (source_thread_id is None or record.source_thread.thread_id == source_thread_id)
                        and ((session_id is None and thread_id is None) or any(
                            (session_id is None or ref.session_id == session_id)
                            and (thread_id is None or ref.thread_id == thread_id)
                            for ref in refs
                        ))
                    )

                records = tuple(record for record in self._store.all() if matches(record))
                await self.recover(record.resource_id for record in records)
                # 一次清单请求不占任何资源锁；只在合并各自观察时短暂持有对应锁。
                records = tuple(self._require_owned_resource_record(record.resource_id) for record in records)
                observed = index_resources(await self._transport.list_resources(), self.workspace_id, self._store.ids(), self.kind)
                result: list[ResourceSnapshot] = []
                for before in records:
                    async with self._resource_lock(before.resource_id):
                        record = self._require_owned_resource_record(before.resource_id)
                        if not matches(record) or (record.status == "deleted" and record.reconciliation_status == "verified"):
                            continue
                        # 同版本外部状态观察、未知占用等也能改变记录，不能只比较owner_revision。
                        if record != before:
                            raise ResourceConflict(f"外部资源列表读取期间状态已改变；请刷新: {record.resource_id}")
                        runtime = observed.get(record.resource_id)
                        observe_resource(record, runtime, self.kind)
                        # TODO：逐项持久化仍重写全量JSON，后续测量大清单成本再优化写盘。
                        self._store.save(record)
                        self._sync_ledger(record)
                        result.append(resource_projection(record, runtime, self.kind))
                return sorted(result, key=lambda item: str(item["updated_at"]), reverse=True)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源列表等待超过 30 秒: workspace_id={self.workspace_id} "
                f"kind={self.kind}；保留已提交观察与原占用，未取得完整清单"
            ) from error

    async def get_resource(self, resource_id: str) -> ResourceSnapshot:
        # 详情的恢复、资源锁和清单共用一次预算，不在阶段切换时重新计时。
        # TODO(5.4)：同步存储与不合作取消仍须由原执行owner核实收敛。
        deadline = timeout(30)
        try:
            async with deadline:
                await self.recover((resource_id,))
                async with self._resource_lock(resource_id):
                    record = self._require_owned_resource_record(resource_id)
                    observed = index_resources(await self._transport.list_resources(), self.workspace_id, self._store.ids(), self.kind)
                    runtime = observed.get(resource_id)
                    observe_resource(record, runtime, self.kind)
                    self._store.save(record)
                    self._sync_ledger(record)
                    return resource_projection(record, runtime, self.kind)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源详情等待超过 30 秒: workspace_id={self.workspace_id} "
                f"kind={self.kind}；保留已提交恢复与原占用，未取得本次观察"
            ) from error

    async def command(
        self,
        resource_id: str,
        *,
        action: str,
        expected_revision: int,
        holder_id: str,
        operation_id: str,
        thread: ThreadRef | None = None,
    ) -> ResourceSnapshot:
        # 管理入口只取得一次预算，恢复、准入、锁及多次传输不重新计时。
        # TODO(5.4)：同步写盘与不合作取消不能由异步预算抢占，外部终态仍须原owner核实。
        deadline = timeout(30)
        try:
            async with deadline:
                await self.recover((resource_id,))
                if action in {"attach", "detach"}:
                    if thread is None:
                        raise ValueError(f"外部资源 {action} 缺少精确 Thread")
                    async with self.admit_thread(thread.session_id, thread.thread_id):
                        return await self._command(
                            resource_id, action, expected_revision, thread, holder_id, operation_id,
                        )
                return await self._command(
                    resource_id, action, expected_revision, thread, holder_id, operation_id,
                )
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源管理命令等待超过 30 秒: workspace_id={self.workspace_id} "
                f"kind={self.kind}, resource_id={resource_id}, action={action}, "
                f"holder_id={holder_id}, operation_id={operation_id}；"
                "保留未核实状态与原占用，未确认外部操作停止"
            ) from error

    def fork_attach_receipt(
        self, resource_id: str, *, creation_key: str, thread: ThreadRef, expected_revision: int,
    ) -> ForkAttachReceipt | None:
        """只读原 operation 历史，不将当前关联误认为本次提交证明。"""
        record = self._store.get(resource_id)
        receipt = None if record is None else record.fork_attachments.get(creation_key)
        if receipt is not None and (receipt.target_thread, receipt.expected_revision) != (thread, expected_revision):
            raise ResourceConflict("fork attach operation 的冻结目标或 expected_revision 冲突")
        return None if receipt is None else replace(receipt)

    def _fork_attach_operation_id(self, resource_id: str, creation_key: str) -> str:
        return f"fork-attach:{creation_key}:{self.kind}:{resource_id}"

    def _release_committed_fork_leases(self, record: OwnedResourceRecord) -> None:
        for receipt in record.fork_attachments.values():
            if receipt.state != "committed":
                continue
            if receipt.lease_id is None:
                raise RuntimeError("已提交 fork attach 回执缺少精确 operation lease 身份")
            lease = self._leases.get_lease(receipt.lease_id)
            if lease is None:
                raise RuntimeError("已提交 fork attach 回执缺少原 operation lease")
            if (lease.resource_id, lease.turn_stream_id, lease.operation_id, lease.holder_session_id, lease.holder_thread_id) != (
                record.resource_id, f"fork:{receipt.creation_key}",
                self._fork_attach_operation_id(record.resource_id, receipt.creation_key),
                receipt.target_thread.session_id, receipt.target_thread.thread_id,
            ):
                raise RuntimeError("fork attach 回执与原 operation lease 身份冲突")
            if lease.status in {"active", "reconcile_required"}:
                # 只完成这一条已提交的操作；资源本身仍存活，不使用资源终态 settle。
                self._leases.release(lease.lease_id)

    async def attach_for_fork(
        self, resource_id: str, *, creation_key: str, expected_revision: int, thread: ThreadRef,
    ) -> tuple[ResourceSnapshot, ForkAttachReceipt]:
        """仅为 fork 的发布后恢复保留最小回执，实际 attach 共用原命令写入方。"""
        await self.recover((resource_id,))
        async with self._resource_lock(resource_id):
            existing = self.fork_attach_receipt(
                resource_id, creation_key=creation_key, thread=thread, expected_revision=expected_revision,
            )
        if existing is not None:
            return await self.get_resource(resource_id), existing
        async with self.admit_thread(thread.session_id, thread.thread_id), self._resource_lock(resource_id):
            record = self._require_owned_resource_record(resource_id)
            existing = self.fork_attach_receipt(
                resource_id, creation_key=creation_key, thread=thread, expected_revision=expected_revision,
            )
            if existing is not None:
                return resource_projection(record, None, self.kind), existing
            receipt = ForkAttachReceipt(creation_key, thread, expected_revision, "rejected")
            try:
                decision = check_resource_target(
                    resource_target_facts(resource_projection(record, None, self.kind)),
                    ResourceTargetContext(storage_domain_id=self.workspace_id, workspace_id=self.workspace_id,
                                          owner_session_id=thread.session_id, owner_thread_id=thread.thread_id),
                    choice="reuse_reference",
                )
                if not decision.available:
                    raise ResourceConflict(decision.reason)
                snapshot = await self._command_record(
                    record, "attach", expected_revision, thread, f"fork:{creation_key}",
                    self._fork_attach_operation_id(resource_id, creation_key), fork_attachment=receipt,
                )
            except Exception as error:
                # 已派发的失败保持 dispatching/unknown；绝不因存活快照自动重发。
                record = self._require_owned_resource_record(resource_id)
                receipt = record.fork_attachments.get(creation_key) or ForkAttachReceipt(
                    creation_key, thread, expected_revision, "rejected",
                )
                receipt.error = public_error(error)
                record.fork_attachments[creation_key] = receipt
                self._store.save(record)
                logger.exception("fork attach 未完成: resource_id=%s creation_key=%s", resource_id, creation_key)
                snapshot = resource_projection(record, None, self.kind)
            return snapshot, replace(receipt)

    async def _command(
        self,
        resource_id: str,
        action: str,
        expected_revision: int,
        thread: ThreadRef | None,
        holder_id: str,
        operation_id: str,
    ) -> ResourceSnapshot:
        async with self._resource_lock(resource_id):
            record = self._require_owned_resource_record(resource_id)
            return await self._command_record(
                record, action, expected_revision, thread, holder_id, operation_id
            )

    async def _command_record(
        self,
        record: OwnedResourceRecord,
        action: str,
        expected_revision: int,
        thread: ThreadRef | None,
        holder_id: str,
        operation_id: str,
        lease_id: str | None = None,
        stop_reason: str | None = None,
        fork_attachment: ForkAttachReceipt | None = None,
    ) -> ResourceSnapshot:
        resource_id = record.resource_id
        if record.status == "deleted" and record.reconciliation_status == "verified":
            raise ResourceConflict("外部资源 已删除")
        if record.owner_revision != expected_revision:
            raise ResourceConflict(
                f"外部资源 版本冲突: expected={expected_revision}, actual={record.owner_revision}"
            )
        takeover = False
        if record.reconciliation_status != "verified" and action in {"stop", "delete"}:
            # 已停止时只对账；仍未知时必须以新的管理身份沿原停止链终止。
            runtime = await self._transport.get_resource(resource_id)
            self._reconcile_stopped_record(record, runtime)
            if record.reconciliation_status == "verified" and (
                record.status == "deleted" or (action == "stop" and record.status in STOPPED_STATUSES)
            ):
                return resource_projection(record, runtime, self.kind)
            if record.reconciliation_status != "verified":
                observe_resource(record, runtime, self.kind)
                if runtime["transport_owner_revision"] != record.owner_revision:
                    raise ResourceConflict("外部资源 接管观察版本与当前 owner 不一致；请刷新资源")
                if lease_id is not None:
                    raise ResourceConflict("外部资源 接管必须取得新的管理 operation 身份")
                if action == "stop" and record.status in {"deleted", "deleting"}:
                    raise ResourceConflict("外部资源 删除尚未核实；请显式完成 delete")
                takeover = True
        if not takeover:
            self._verified(record)
        leases = [
            lease
            for lease in self._leases.leases_for_resource(resource_id)
            if lease.lease_id != lease_id
        ]
        if action == "detach":
            if any(
                (lease.holder_session_id, lease.holder_thread_id)
                == (thread.session_id, thread.thread_id)
                for lease in leases
            ):
                raise ResourceConflict(
                    "目标 Thread 仍持有有效或待核实 operation lease"
                )
        elif action in {"stop", "delete"}:
            if any(lease.status == "active" for lease in leases) or (leases and not takeover):
                raise ResourceConflict("外部资源 仍有有效或待核实 operation lease")
            if any((lease.turn_stream_id, lease.operation_id) == (holder_id, operation_id) for lease in leases):
                raise ResourceConflict("外部资源 原 operation 结果尚未核实；请发起新的管理操作")
        if action == "delete" and record.thread_associations:
            raise ResourceConflict("外部资源 仍有关联；请先解除关联")
        if (
            action == "attach"
            and record.retention_scope == "thread"
            and thread != record.source_thread
        ):
            raise ResourceConflict(
                "临时 外部资源 只能关联来源 Thread；请先提升为 workspace"
            )
        if action not in {"promote", "attach", "detach", "stop", "delete"}:
            raise ValueError(f"外部资源 不支持操作: {action}")
        with self._management_lease(
            record, thread, holder_id, operation_id, lease_id, allow_unverified=takeover,
        ) as acquired_lease_id:
            if fork_attachment is not None:
                fork_attachment.state = "dispatching"
                fork_attachment.lease_id = acquired_lease_id
                record.fork_attachments[fork_attachment.creation_key] = fork_attachment
                self._store.save(record)
            committed = False
            try:
                if fork_attachment is not None:
                    await observe_phase(fork_attachment.creation_key, f"fork.live.dispatched.{self.kind}")
                if action in {"stop", "delete"}:
                    return await self._stop_or_delete(record, delete=action == "delete", reason=stop_reason, lease_id=acquired_lease_id)
                await self._advance_revision(record)
                runtime = await self._transport.get_resource(resource_id)
                observe_resource(record, runtime, self.kind)
                self._verified(record)
                if action == "promote":
                    record.retention_scope = "workspace"
                    record.unattached_since = None
                elif action == "attach":
                    if thread not in record.thread_associations:
                        record.thread_associations.append(thread)
                    record.unattached_since = None
                else:
                    record.thread_associations = [
                        ref for ref in record.thread_associations if ref != thread
                    ]
                    if (
                        record.retention_scope == "thread"
                        and not record.thread_associations
                    ):
                        record.unattached_since = record.unattached_since or self._now()
                record.updated_at = self._now()
                if fork_attachment is not None:
                    fork_attachment.state = "committed"
                    fork_attachment.committed_revision = record.owner_revision
                self._store.save(record)
                committed = True
                if fork_attachment is not None:
                    await observe_phase(fork_attachment.creation_key, f"fork.live.committed.{self.kind}")
                self._sync_ledger(record)
                return resource_projection(record, runtime, self.kind)
            except BaseException as error:
                if fork_attachment is not None and committed:
                    raise
                self._unknown(record, error, lease_id=acquired_lease_id)
                raise

    async def _advance_revision(self, record: OwnedResourceRecord, *, for_stop: bool = False) -> None:
        record.owner_revision += 1
        record.updated_at = self._now()
        self._store.save(record)
        await self._transport.sync_owner_revision(
            record.resource_id, record.owner_revision, for_stop=for_stop,
        )

    async def _stop_or_delete(
        self, record: OwnedResourceRecord, *, delete: bool, lease_id: str, reason: str | None = None,
    ) -> ResourceSnapshot:
        record.status = "deleting" if delete else "stopping"
        record.updated_at = self._now()
        self._store.save(record)
        try:
            # 停止先撤销旧授权；真实执行的排空由外部owner在关闭运行时后核实。
            await self._advance_revision(record, for_stop=True)
            result = await (
                self._transport.delete_resource(record.resource_id)
                if delete
                else self._transport.stop_resource(record.resource_id, reason=reason)
            )
            runtime = result.get(self.kind)
            if not isinstance(runtime, dict):
                raise TypeError("外部资源 owner 未收到停止/删除的完整外部回执")
            if type(runtime.get("transport_owner_revision")) is not int or runtime["transport_owner_revision"] != record.owner_revision:
                raise ResourceConflict("外部资源 停止/删除回执版本与本次操作不一致")
            observe_resource(record, runtime, self.kind)
            if runtime.get("reconciliation_status") != "verified" or record.status not in STOPPED_STATUSES or (delete and record.status != "deleted"):
                raise RuntimeError(
                    f"外部资源 停止/删除结果尚未核实: status={record.status}"
                )
            record.reconciliation_status = "verified"
            record.error = None
            record.unattached_since = None if delete else record.unattached_since
            self._store.save(record)
            self._sync_ledger(record)
            for pending in self._leases.leases_for_resource(record.resource_id):
                if pending.status == "reconcile_required":
                    self._leases.settle(pending.lease_id)
            return resource_projection(record, runtime, self.kind)
        except BaseException as error:
            self._unknown(record, error, lease_id=lease_id)
            raise

    async def release_thread(
        self, session_id: str, thread_id: str, generation: int
    ) -> None:
        """Runtime lease 排空后的同代通知；临时孤儿也按不可变来源立即清理。"""
        ref = ThreadRef(session_id, thread_id)
        if not self._thread_release_fence(session_id, thread_id, generation):
            raise ResourceConflict(
                "外部资源 Thread 释放不属于当前卸载中的 runtime generation"
            )
        if not self._store.ids():
            return
        await self._release_refs(
            lambda item: item == ref,
            holder_id=f"thread-unload:{session_id}/{thread_id}:{generation}",
            operation_id=f"unload:{generation}",
        )

    async def cleanup_session(self, session_id: str) -> int:
        """由关闭准入后的 Session 删除 drain 调用，不重新进入其生命周期 gate。"""
        if not self._store.ids():
            return 0
        return await self._release_refs(
            lambda ref: ref.session_id == session_id,
            holder_id=f"session-delete:{session_id}",
            operation_id=f"delete:{session_id}",
        )

    async def _release_refs(
        self,
        matches: Callable[[ThreadRef], bool],
        *,
        holder_id: str,
        operation_id: str,
    ) -> int:
        locked: dict[str, asyncio.Lock] = {}
        tasks: list[asyncio.Task[Exception | None]] = []

        async def release(record: OwnedResourceRecord) -> Exception | None:
            try:
                if record.status == "deleted":
                    return None
                with self._management_lease(record, None, holder_id, operation_id) as lease_id:
                    try:
                        if record.retention_scope == "thread":
                            record.thread_associations = []
                            await self._stop_or_delete(record, delete=True, lease_id=lease_id)
                        else:
                            await self._advance_revision(record)
                            record.thread_associations = [
                                ref for ref in record.thread_associations if not matches(ref)
                            ]
                            record.updated_at = self._now()
                            self._store.save(record)
                    except BaseException as error:
                        self._unknown(record, error, lease_id=lease_id)
                        raise
                return None
            except Exception as error:  # noqa: BLE001 - 各资源独立收敛后保留原错误。
                return error
            finally:
                # 完整preflight后各任务接管自己的原锁，成功项不等其它网络回执。
                locked.pop(record.resource_id).release()

        # 准备、全量检查与各资源释放共用一次预算；超时不是停止证明。
        # TODO：同步写盘、不合作取消及外部终止仍须沿原owner核实。
        deadline = timeout(30)
        try:
            async with deadline:
                await self.recover(())
                affected = [
                    record
                    for record in self._store.all()
                    if (record.status != "deleted" or record.reconciliation_status != "verified")
                    and (
                        any(matches(ref) for ref in record.thread_associations)
                        or (record.retention_scope == "thread" and matches(record.source_thread))
                    )
                ]
                await self.recover(record.resource_id for record in affected)
                # 重试只核原终态，不重发未知停止；网络观察不持有其它资源锁。
                for record in affected:
                    if record.reconciliation_status != "verified":
                        async with self._resource_lock(record.resource_id):
                            current = self._require_owned_resource_record(record.resource_id)
                            if current.reconciliation_status != "verified":
                                runtime = await self._transport.get_resource(record.resource_id)
                                self._reconcile_stopped_record(current, runtime)
                for resource_id in sorted(record.resource_id for record in affected):
                    lock = self._resource_lock(resource_id)
                    await lock.acquire()
                    locked[resource_id] = lock
                affected = [self._require_owned_resource_record(record.resource_id) for record in affected]
                # 从全量preflight到任务接管之间不让出事件循环，避免新lease插入。
                for record in affected:
                    self._verified(record)
                    if any(
                        lease.holder_session_id is None
                        or matches(ThreadRef(lease.holder_session_id, lease.holder_thread_id))
                        for lease in self._leases.leases_for_resource(record.resource_id)
                    ):
                        raise ResourceConflict(
                            f"Thread 释放仍有未结清 外部资源 operation: {record.resource_id}"
                        )
                tasks = [asyncio.create_task(release(record)) for record in affected]
                results = await asyncio.gather(*tasks)
                errors = [result for result in results if isinstance(result, Exception)]
                if len(errors) == 1:
                    raise errors[0]
                if errors:
                    raise ExceptionGroup("外部资源关联释放存在未收敛资源", errors)
                return len(affected)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源关联释放等待超过 30 秒: workspace_id={self.workspace_id} kind={self.kind} "
                f"holder_id={holder_id}；保留未核实记录与原占用，未确认外部操作停止"
            ) from error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for lock in locked.values():
                lock.release()

    async def sweep_orphans(self) -> list[ResourceSnapshot]:
        if not self._store.ids():
            return []

        async def reclaim(resource_id: str) -> ResourceSnapshot | Exception | None:
            try:
                # 冷恢复与删除均按原资源锁推进，单项同步或回执等待不占住整批。
                await self.recover((resource_id,))
                async with self._resource_lock(resource_id):
                    record = self._require_owned_resource_record(resource_id)
                    if (
                        record.status == "deleted"
                        or record.retention_scope != "thread"
                        or record.thread_associations
                        or record.unattached_since is None
                        or (
                            self._clock() - datetime.fromisoformat(record.unattached_since)
                        ).total_seconds()
                        < ORPHAN_GRACE_SECONDS
                        or self._leases.leases_for_resource(record.resource_id)
                    ):
                        return None
                    self._verified(record)
                    with self._management_lease(
                        record, None, f"orphan-reclaim:{record.resource_id}",
                        f"reclaim:{record.unattached_since}",
                    ) as lease_id:
                        return await self._stop_or_delete(record, delete=True, lease_id=lease_id)
            except Exception as error:  # noqa: BLE001 - 同批其它资源收敛后按原顺序汇总。
                return error

        # 一次回收的准备、恢复、锁及删除共用入口预算；取消不等于终止证明。
        # TODO：同步阻塞与不响应取消的外部操作仍须原owner终止核实。
        deadline = timeout(30)
        try:
            async with deadline:
                await self.recover(())
                tasks = [asyncio.create_task(reclaim(resource_id)) for resource_id in self._store.ids()]
                try:
                    results = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                errors = [result for result in results if isinstance(result, Exception)]
                if errors:
                    raise ExceptionGroup("外部资源 孤儿回收存在未收敛资源", errors)
                return [result for result in results if isinstance(result, dict)]
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源孤儿回收等待超过 30 秒: workspace_id={self.workspace_id} kind={self.kind}；"
                "保留未核实记录与原占用，未确认外部操作停止"
            ) from error

    async def transport_grant(
        self,
        resource_id: str,
        *,
        expected_revision: int,
        thread: ThreadRef | None = None,
        holder_id: str,
        operation_id: str,
    ) -> ResourceSnapshot:
        # 原预检、恢复、准入和签发共用预算，派发后的未知结果仍由原lease链保留。
        # TODO(5.4)：同步写盘与不合作取消不能由此预算抢占，外部终态仍须原owner核实。
        deadline = timeout(30)
        try:
            async with deadline:
                # 已核实的非运行资源不再请求外部服务，终态也不因晚到授权变成 unknown。
                async with self._resource_lock(resource_id):
                    record = self._require_owned_resource_record(resource_id)
                    if record.reconciliation_status == "verified" and record.status != "running":
                        raise ResourceConflict("外部资源 不在运行状态，不能签发传输授权")
                await self.recover((resource_id,))
                if thread is not None:
                    async with self.admit_thread(thread.session_id, thread.thread_id):
                        return await self._grant(
                            resource_id, expected_revision, thread, holder_id, operation_id
                        )
                return await self._grant(
                    resource_id, expected_revision, None, holder_id, operation_id
                )
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise TimeoutError(
                f"外部资源传输授权等待超过 30 秒: resource_id={resource_id}；"
                "保留未核实记录与原占用，未确认外部授权或资源已经停止"
            ) from error

    async def _grant(
        self,
        resource_id: str,
        revision: int,
        thread: ThreadRef | None,
        holder_id: str,
        operation_id: str,
    ) -> ResourceSnapshot:
        async with self._resource_lock(resource_id):
            record = self._require_owned_resource_record(resource_id)
            self._verified(record)
            if record.status != "running":
                raise ResourceConflict("外部资源 不在运行状态，不能签发传输授权")
            if record.owner_revision != revision:
                raise ResourceConflict(
                    "外部资源 传输授权版本已过期，请刷新资源"
                )
            if thread is None and record.retention_scope != "workspace":
                raise ResourceConflict(
                    "临时 外部资源 传输必须绑定有效关联 Thread"
                )
            if thread is not None and thread not in record.thread_associations:
                raise ResourceConflict("Thread 尚未关联 外部资源")
            with self._management_lease(record, thread, holder_id, operation_id) as lease_id:
                try:
                    runtime = await self._transport.get_resource(resource_id)
                    self._observe_grant_snapshot(record, runtime)
                except BaseException as error:
                    self._unknown(record, error, lease_id=lease_id)
                    raise
                if record.status != "running" or record.owner_revision != revision:
                    raise ResourceConflict("外部资源 运行状态或传输授权版本已改变，请刷新资源")
                try:
                    grant = await self._transport.issue_transport_grant(
                        resource_id,
                        owner_revision=revision,
                        association=None
                        if thread is None
                        else f"{thread.session_id}/{thread.thread_id}",
                    )
                except ResourceTransportGrantRejected as rejection:
                    try:
                        snapshot = rejection.snapshot
                        if snapshot.get("status") == "running" and snapshot.get("transport_owner_revision") == revision:
                            raise ValueError("外部资源 传输拒绝快照没有状态或版本冲突")
                        self._observe_grant_snapshot(record, snapshot)
                    except BaseException as error:
                        self._unknown(record, error, lease_id=lease_id)
                        raise
                    raise ResourceConflict("外部资源 运行状态或传输授权版本已改变，请刷新资源") from rejection
                except BaseException as error:
                    self._unknown(record, error, lease_id=lease_id)
                    raise
                return {**grant, "resource": resource_projection(record, runtime, self.kind)}

    def _observe_grant_snapshot(
        self, record: OwnedResourceRecord, runtime: ResourceSnapshot,
    ) -> None:
        revision = runtime.get("transport_owner_revision")
        if (
            runtime.get("reconciliation_status") != "verified"
            or not isinstance(runtime.get("status"), str)
            or not runtime["status"]
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < record.owner_revision
        ):
            raise ValueError("外部资源 传输授权快照未核实或版本已经过期")
        # 先由同一事实边界核对资源与工作区身份，再接受当前版本；绝不回退 owner。
        observe_resource(record, runtime, self.kind)
        record.owner_revision = revision
        self._store.save(record)
        self._sync_ledger(record)

    def _reconcile_stopped_record(
        self,
        record: OwnedResourceRecord,
        runtime: ResourceSnapshot | None,
        *,
        pending_leases: list[ResourceLease] | None = None,
    ) -> list[ResourceLease]:
        pending = (
            self._leases.leases_for_resource(record.resource_id)
            if pending_leases is None
            else pending_leases
        )
        if any(lease.status != "reconcile_required" for lease in pending):
            return pending
        # 只接受当前身份、非旧版本且已核实停止的外部事实；不声称旧操作成功。
        if runtime is None or runtime.get("reconciliation_status") != "verified" or runtime.get("status") not in STOPPED_STATUSES:
            return pending
        revision = runtime.get("transport_owner_revision")
        if type(revision) is not int or revision < record.owner_revision:
            return pending
        observe_resource(record, runtime, self.kind)
        record.reconciliation_status = "verified"
        record.owner_revision = revision
        record.error = None
        self._store.save(record)
        for lease in pending:
            self._leases.settle(lease.lease_id)
        self._sync_ledger(record)
        return []

    def _resource_lock(self, resource_id: str) -> asyncio.Lock:
        self._require_owned_resource_record(resource_id)
        return self._resource_locks[resource_id]

    def _require_owned_resource_record(self, resource_id: str) -> OwnedResourceRecord:
        record = self._store.get(resource_id)
        if record is None:
            raise KeyError(f"外部资源 owner 资源不存在: {resource_id}")
        # owner 的 unknown 写盘可能失败；原账本的未核实事实不能被普通读取消除。
        external = self._leases.get(resource_id)
        if record.reconciliation_status == "verified" and external is not None and external.status in {"unknown", "orphaned"}:
            record.reconciliation_status = "reconcile_required"
            record.error = record.error or "原资源账本的运行事实或 operation 尚未核实"
        return record

    def _acquire(
        self,
        record: OwnedResourceRecord,
        ref: ThreadRef | None,
        holder_id: str,
        operation_id: str,
        *,
        allow_unverified: bool = False,
    ) -> str:
        return self._leases.acquire_operation(
            resource_id=record.resource_id,
            turn_stream_id=holder_id,
            operation_id=operation_id,
            holder_session_id=None if ref is None else ref.session_id,
            holder_thread_id=None if ref is None else ref.thread_id,
            lease_id=f"{self.kind}:{create_uuid_hex()}",
            allow_stopped=True,
            allow_unverified=allow_unverified,
        ).lease_id

    @contextmanager
    def _management_lease(
        self,
        record: OwnedResourceRecord,
        ref: ThreadRef | None,
        holder_id: str,
        operation_id: str,
        lease_id: str | None = None,
        *,
        allow_unverified: bool = False,
    ) -> Iterator[str]:
        if lease_id is not None:
            lease = self._leases.get_lease(lease_id)
            if (
                lease is None
                or lease.status != "active"
                or (lease.resource_id, lease.turn_stream_id, lease.operation_id)
                != (record.resource_id, holder_id, operation_id)
            ):
                raise ResourceConflict("外部资源 操作引用不是当前有效 lease")
        acquired = lease_id or self._acquire(
            record, ref, holder_id, operation_id, allow_unverified=allow_unverified,
        )
        try:
            yield acquired
        finally:
            if lease_id is None:
                self._release_operation_lease(acquired)

    def _release_operation_lease(self, lease_id: str) -> None:
        lease = self._leases.get_lease(lease_id)
        if lease is None:
            raise KeyError(f"外部资源 operation 缺少原 lease: {lease_id}")
        # 未知占用交给终态对账；迟到收尾也不能把 settled 回写成普通 released。
        if lease.status == "active":
            self._leases.release(lease_id)

    @staticmethod
    def _verified(record: OwnedResourceRecord) -> None:
        if record.reconciliation_status != "verified":
            raise ResourceConflict(
                f"外部资源 尚待 owner 核实: {record.resource_id}, {record.error}"
            )

    def _unknown(self, record: OwnedResourceRecord, error: BaseException, *, lease_id: str | None = None) -> None:
        # 丢弃未发布草稿，失败的关联、scope、版本与终态不能混入 unknown 回执。
        record = self._require_owned_resource_record(record.resource_id)
        record.reconciliation_status = "unknown"
        logger.error(
            "外部资源 操作结果未核实: resource_id=%s",
            record.resource_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        record.error = public_error(error)
        record.updated_at = self._now()
        # 先保护本次占用；unknown 记录再次写盘失败也不能由普通 finally 释放。
        self._leases.update_external_state(
            record.resource_id, lifetime_scope=record.retention_scope,
            status="unknown", unresolved_lease_id=lease_id,
        )
        self._store.save(record)

    def _sync_ledger(self, record: OwnedResourceRecord) -> None:
        self._leases.update_external_state(
            record.resource_id,
            lifetime_scope=record.retention_scope,
            status=lease_status(record),
        )

    def _now(self) -> str:
        return self._clock().isoformat()
