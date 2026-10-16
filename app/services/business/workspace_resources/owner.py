"""两个领域 owner 共用的真实保留、关联、lease 与释放规则。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.workspace_resources import (
    ResourceKind,
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
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
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
        self._lock = asyncio.Lock()
        self._recovered = False

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

    async def recover(self) -> None:
        async with self._lock:
            if self._recovered:
                return
            observed = index_resources(await self._transport.list_resources(), self.workspace_id, self._store.records, self.kind)
            statuses: dict[str, str] = {}
            for record in self._store.records.values():
                self._leases.register_external(
                    resource_id=record.resource_id,
                    kind=self.lease_kind,
                    lifetime_scope=record.retention_scope,
                )
                runtime = observed.get(record.resource_id)
                observe_resource(record, runtime, self.kind)
                statuses[record.resource_id] = lease_status(record)
            # 每个领域只对账自身 lease，不修改另一 owner 的运行事实。
            self._leases.reconcile(statuses, kind=self.lease_kind)
            for record in self._store.records.values():
                self._release_committed_fork_leases(record)
                self._reconcile_stopped_record(record, observed.get(record.resource_id))
                if self._leases.leases_for_resource(record.resource_id):
                    record.reconciliation_status = "reconcile_required"
                    record.error = "前次 operation 结果尚未核实；未自动重放或结清 lease"
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
                    self._store.persist()
                    try:
                        await self._transport.sync_owner_revision(
                            record.resource_id, record.owner_revision
                        )
                    except BaseException as error:
                        self._unknown(record, error)
                        raise
            self._store.persist()
            self._recovered = True

    async def _create_record(
        self, *, ref: ThreadRef, retention_scope: RetentionScope, title: str,
        holder_id: str, operation_id: str, created_by_turn_id: str | None,
        lease_refs: dict[str, str],
        create: Callable[[OwnedResourceRecord], Awaitable[ResourceSnapshot]],
    ) -> ResourceSnapshot:
        async with self._lock:
            stamp = self._now()
            prefix = "term" if self.kind == "terminal" else "browser"
            record = OwnedResourceRecord(
                resource_id=f"{prefix}_{create_uuid_hex()}",
                workspace_id=self.workspace_id, source_thread=ref,
                retention_scope=retention_scope, title=title,
                created_at=stamp, updated_at=stamp, thread_associations=[ref],
            )
            self._store.records[record.resource_id] = record
            self._store.persist()
            self._leases.register_external(
                resource_id=record.resource_id, kind=self.lease_kind,
                lifetime_scope=retention_scope, created_by_turn_id=created_by_turn_id,
            )
            lease_refs[record.resource_id] = self._acquire(record, ref, holder_id, operation_id)
            try:
                runtime = await create(record)
                observe_resource(record, runtime, self.kind)
                self._verified(record)
                self._store.persist()
                self._sync_ledger(record)
                return resource_projection(record, runtime, self.kind)
            except BaseException as error:
                self._unknown(record, error)
                raise

    async def list_resources(
        self,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        retention_scope: RetentionScope | None = None,
        source_session_id: str | None = None,
        source_thread_id: str | None = None,
    ) -> list[ResourceSnapshot]:
        await self.recover()
        async with self._lock:
            observed = index_resources(await self._transport.list_resources(), self.workspace_id, self._store.records, self.kind)
            result: list[ResourceSnapshot] = []
            for record in self._store.records.values():
                observe_resource(record, observed.get(record.resource_id), self.kind)
                self._sync_ledger(record)
                if record.status == "deleted" and record.reconciliation_status == "verified":
                    continue
                if (
                    retention_scope is not None
                    and record.retention_scope != retention_scope
                ):
                    continue
                if (
                    source_session_id is not None
                    and record.source_thread.session_id != source_session_id
                ):
                    continue
                if (
                    source_thread_id is not None
                    and record.source_thread.thread_id != source_thread_id
                ):
                    continue
                if session_id is not None or thread_id is not None:
                    refs = record.thread_associations
                    if record.retention_scope == "thread" and not refs:
                        refs = [record.source_thread]
                    if not any(
                        (session_id is None or ref.session_id == session_id)
                        and (thread_id is None or ref.thread_id == thread_id)
                        for ref in refs
                    ):
                        continue
                result.append(
                    resource_projection(record, observed.get(record.resource_id), self.kind)
                )
            self._store.persist()
            return sorted(
                result, key=lambda item: str(item["updated_at"]), reverse=True
            )

    async def get_resource(self, resource_id: str) -> ResourceSnapshot:
        await self.recover()
        async with self._lock:
            record = self._require(resource_id)
            observed = index_resources(await self._transport.list_resources(), self.workspace_id, self._store.records, self.kind)
            runtime = observed.get(resource_id)
            observe_resource(record, runtime, self.kind)
            self._sync_ledger(record)
            self._store.persist()
            return resource_projection(record, runtime, self.kind)

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
        await self.recover()
        if action in {"attach", "detach"}:
            if thread is None:
                raise ValueError(f"外部资源 {action} 缺少精确 Thread")
            async with self.admit_thread(thread.session_id, thread.thread_id):
                return await self._command(
                    resource_id,
                    action,
                    expected_revision,
                    thread,
                    holder_id,
                    operation_id,
                )
        return await self._command(
            resource_id, action, expected_revision, thread, holder_id, operation_id
        )

    def fork_attach_receipt(
        self, resource_id: str, *, creation_key: str, thread: ThreadRef, expected_revision: int,
    ) -> ForkAttachReceipt | None:
        """只读原 operation 历史，不将当前关联误认为本次提交证明。"""
        record = self._store.records.get(resource_id)
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
                self._leases.release(lease.lease_id, reason="fork_attach_committed")

    async def attach_for_fork(
        self, resource_id: str, *, creation_key: str, expected_revision: int, thread: ThreadRef,
    ) -> tuple[ResourceSnapshot, ForkAttachReceipt]:
        """仅为 fork 的发布后恢复保留最小回执，实际 attach 共用原命令写入方。"""
        await self.recover()
        async with self._lock:
            existing = self.fork_attach_receipt(
                resource_id, creation_key=creation_key, thread=thread, expected_revision=expected_revision,
            )
        if existing is not None:
            return await self.get_resource(resource_id), existing
        async with self.admit_thread(thread.session_id, thread.thread_id), self._lock:
            record = self._require(resource_id)
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
                receipt.error = public_error(error)
                record.fork_attachments[creation_key] = receipt
                self._store.persist()
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
        async with self._lock:
            record = self._require(resource_id)
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
        if record.reconciliation_status != "verified" and action in {"stop", "delete"}:
            # 显式重试先核终态；运行中/未知的前次副作用绝不重放。
            runtime = await self._transport.get_resource(resource_id)
            self._reconcile_stopped_record(record, runtime)
            self._store.persist()
            self._verified(record)
            if record.status == "deleted" or (action == "stop" and record.status in STOPPED_STATUSES):
                return resource_projection(record, runtime, self.kind)
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
        elif action in {"stop", "delete"} and leases:
            raise ResourceConflict("外部资源 仍有有效或待核实 operation lease")
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
        with self._management_lease(record, thread, holder_id, operation_id, lease_id) as acquired_lease_id:
            associations_before = list(record.thread_associations)
            if fork_attachment is not None:
                fork_attachment.state = "dispatching"
                fork_attachment.lease_id = acquired_lease_id
                record.fork_attachments[fork_attachment.creation_key] = fork_attachment
                self._store.persist()
            committed = False
            try:
                if fork_attachment is not None:
                    await observe_phase(fork_attachment.creation_key, f"fork.live.dispatched.{self.kind}")
                if action in {"stop", "delete"}:
                    return await self._stop_or_delete(record, delete=action == "delete", reason=stop_reason)
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
                self._store.persist()
                committed = True
                if fork_attachment is not None:
                    await observe_phase(fork_attachment.creation_key, f"fork.live.committed.{self.kind}")
                self._sync_ledger(record)
                return resource_projection(record, runtime, self.kind)
            except BaseException as error:
                if fork_attachment is not None:
                    if committed:
                        raise
                    record.thread_associations = associations_before
                    fork_attachment.state = "dispatching"
                    fork_attachment.committed_revision = None
                self._unknown(record, error)
                raise

    async def _advance_revision(self, record: OwnedResourceRecord) -> None:
        record.owner_revision += 1
        record.updated_at = self._now()
        self._store.persist()
        await self._transport.sync_owner_revision(
            record.resource_id, record.owner_revision
        )

    async def _stop_or_delete(
        self, record: OwnedResourceRecord, *, delete: bool, reason: str | None = None,
    ) -> ResourceSnapshot:
        record.status = "deleting" if delete else "stopping"
        record.updated_at = self._now()
        self._store.persist()
        try:
            await self._advance_revision(record)
            result = await (
                self._transport.delete_resource(record.resource_id)
                if delete
                else self._transport.stop_resource(record.resource_id, reason=reason)
            )
            runtime = result.get(self.kind)
            if not isinstance(runtime, dict):
                raise TypeError("外部资源 owner 未收到停止/删除的完整外部回执")
            observe_resource(record, runtime, self.kind)
            self._verified(record)
            if record.status not in STOPPED_STATUSES or (delete and record.status != "deleted"):
                raise RuntimeError(
                    f"外部资源 停止/删除结果尚未核实: status={record.status}"
                )
            record.unattached_since = None if delete else record.unattached_since
            self._store.persist()
            self._sync_ledger(record)
            return resource_projection(record, runtime, self.kind)
        except BaseException as error:
            self._unknown(record, error)
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
        if not self._store.records:
            return
        await self.recover()
        async with self._lock:
            await self._release_refs(
                lambda item: item == ref,
                holder_id=f"thread-unload:{session_id}/{thread_id}:{generation}",
                operation_id=f"unload:{generation}",
            )

    async def cleanup_session(self, session_id: str) -> int:
        """由关闭准入后的 Session 删除 drain 调用，不重新进入其生命周期 gate。"""
        if not self._store.records:
            return 0
        await self.recover()
        async with self._lock:
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
        affected = [
            record
            for record in self._store.records.values()
            if (record.status != "deleted" or record.reconciliation_status != "verified")
            and (
                any(matches(ref) for ref in record.thread_associations)
                or (
                    record.retention_scope == "thread" and matches(record.source_thread)
                )
            )
        ]
        # 同代清理重试只收敛已核实终态，不重发结果未知的停止/删除。
        for record in affected:
            if record.reconciliation_status != "verified":
                runtime = await self._transport.get_resource(record.resource_id)
                self._reconcile_stopped_record(record, runtime)
        self._store.persist()
        # 先核对整个清理集合，lease 冲突不会留下部分 detach。
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
        for record in affected:
            if record.status == "deleted":
                continue
            with self._management_lease(record, None, holder_id, operation_id):
                try:
                    if record.retention_scope == "thread":
                        record.thread_associations = []
                        await self._stop_or_delete(record, delete=True)
                    else:
                        await self._advance_revision(record)
                        record.thread_associations = [
                            ref
                            for ref in record.thread_associations
                            if not matches(ref)
                        ]
                        record.updated_at = self._now()
                        self._store.persist()
                except BaseException as error:
                    self._unknown(record, error)
                    raise
        return len(affected)

    async def sweep_orphans(self) -> list[ResourceSnapshot]:
        if not self._store.records:
            return []
        await self.recover()
        deleted: list[ResourceSnapshot] = []
        errors: list[Exception] = []
        async with self._lock:
            for record in self._store.records.values():
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
                    continue
                try:
                    self._verified(record)
                    with self._management_lease(
                        record,
                        None,
                        f"orphan-reclaim:{record.resource_id}",
                        f"reclaim:{record.unattached_since}",
                    ):
                        deleted.append(await self._stop_or_delete(record, delete=True))
                except Exception as error:  # noqa: BLE001 - 收敛其它资源后显式汇总失败。
                    errors.append(error)
        if errors:
            raise ExceptionGroup("外部资源 孤儿回收存在未收敛资源", errors)
        return deleted

    async def transport_grant(
        self,
        resource_id: str,
        *,
        expected_revision: int,
        thread: ThreadRef | None = None,
        holder_id: str,
        operation_id: str,
    ) -> ResourceSnapshot:
        # 已核实的非运行资源不再请求外部服务，终态也不因晚到授权变成 unknown。
        async with self._lock:
            record = self._require(resource_id)
            if record.reconciliation_status == "verified" and record.status != "running":
                raise ResourceConflict("外部资源 不在运行状态，不能签发传输授权")
        await self.recover()
        if thread is not None:
            async with self.admit_thread(thread.session_id, thread.thread_id):
                return await self._grant(
                    resource_id, expected_revision, thread, holder_id, operation_id
                )
        return await self._grant(
            resource_id, expected_revision, None, holder_id, operation_id
        )

    async def _grant(
        self,
        resource_id: str,
        revision: int,
        thread: ThreadRef | None,
        holder_id: str,
        operation_id: str,
    ) -> ResourceSnapshot:
        async with self._lock:
            record = self._require(resource_id)
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
            with self._management_lease(record, thread, holder_id, operation_id):
                try:
                    runtime = await self._transport.get_resource(resource_id)
                    self._observe_grant_snapshot(record, runtime)
                except BaseException as error:
                    self._unknown(record, error)
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
                        self._unknown(record, error)
                        raise
                    raise ResourceConflict("外部资源 运行状态或传输授权版本已改变，请刷新资源") from rejection
                except BaseException as error:
                    self._unknown(record, error)
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
        self._store.persist()
        self._sync_ledger(record)

    def _reconcile_stopped_record(
        self, record: OwnedResourceRecord, runtime: ResourceSnapshot | None,
    ) -> None:
        pending = self._leases.leases_for_resource(record.resource_id)
        if any(lease.status != "reconcile_required" for lease in pending):
            return
        # 只接受当前身份、非旧版本且已核实停止的外部事实；不声称旧操作成功。
        if runtime is None or runtime.get("reconciliation_status") != "verified" or runtime.get("status") not in STOPPED_STATUSES:
            return
        revision = runtime.get("transport_owner_revision")
        if not isinstance(revision, int) or revision < record.owner_revision:
            return
        observe_resource(record, runtime, self.kind)
        record.reconciliation_status = "verified"
        record.owner_revision = revision
        for lease in pending:
            self._leases.settle(lease.lease_id)
        record.error = None
        self._sync_ledger(record)

    def _require(self, resource_id: str) -> OwnedResourceRecord:
        record = self._store.records.get(resource_id)
        if record is None:
            raise KeyError(f"外部资源 owner 资源不存在: {resource_id}")
        return record

    def _acquire(
        self,
        record: OwnedResourceRecord,
        ref: ThreadRef | None,
        holder_id: str,
        operation_id: str,
    ) -> str:
        return self._leases.acquire_operation(
            resource_id=record.resource_id,
            turn_stream_id=holder_id,
            operation_id=operation_id,
            holder_session_id=None if ref is None else ref.session_id,
            holder_thread_id=None if ref is None else ref.thread_id,
            lease_id=f"{self.kind}:{create_uuid_hex()}",
            allow_stopped=True,
        ).lease_id

    @contextmanager
    def _management_lease(
        self,
        record: OwnedResourceRecord,
        ref: ThreadRef | None,
        holder_id: str,
        operation_id: str,
        lease_id: str | None = None,
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
        acquired = lease_id or self._acquire(record, ref, holder_id, operation_id)
        try:
            yield acquired
        finally:
            if lease_id is None:
                self._leases.release(acquired)

    @staticmethod
    def _verified(record: OwnedResourceRecord) -> None:
        if record.reconciliation_status != "verified":
            raise ResourceConflict(
                f"外部资源 尚待 owner 核实: {record.resource_id}, {record.error}"
            )

    def _unknown(self, record: OwnedResourceRecord, error: BaseException) -> None:
        record.reconciliation_status = "unknown"
        logger.error(
            "外部资源 操作结果未核实: resource_id=%s",
            record.resource_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        record.error = public_error(error)
        record.updated_at = self._now()
        self._store.persist()
        self._sync_ledger(record)

    def _sync_ledger(self, record: OwnedResourceRecord) -> None:
        self._leases.update_external_state(
            record.resource_id,
            lifetime_scope=record.retention_scope,
            status=lease_status(record),
        )

    def _now(self) -> str:
        return self._clock().isoformat()
