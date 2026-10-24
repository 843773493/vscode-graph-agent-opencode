"""附件消息 pin 的接受结算、崩溃恢复与删除收敛；不建立第二份消息事实。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol

from app.core.phase_observer import observe_phase_sync
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
    transition_copy_attachment_settlement,
)
from app.core.session_lifecycle_gate import SessionOperationLease
from app.domain.itemized.execution_queue import ExecutionQueueSnapshot
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    DirectoryIngestRecord,
)


class AttachmentMessageReader(Protocol):
    """canonical owner 提供接受与使用事实；附件 store 不读取 rollout 数据库。"""

    def attachment_message_references(self, session_id: str, thread_id: str, message_id: str) -> tuple[str, ...] | None: ...

    def attachment_reference_in_use(self, session_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def execution_queue_snapshot(self, session_id: str, thread_id: str) -> ExecutionQueueSnapshot: ...


class AttachmentIngestRecoveryMixin:
    """消息事实由 canonical owner 提供，目录 owner 只协调原上传 pin。"""

    def bind_thread_runtime(self, session_id: str, thread_id: str, binding_token: str) -> None:
        self._catalog.bind_thread_runtime(session_id, thread_id, binding_token, now=self._clock())

    def release_runtime_binding(self, session_id: str, thread_id: str, binding_token: str, *, messages: AttachmentMessageReader) -> tuple[str, ...]:
        """不重取调用方可能持有的 Session gate；核对和解绑间没有 await。"""
        control = self._open_control_store(session_id)
        try:
            if control.list_background_tasks(thread_id=thread_id, active_only=True):
                return ()
            for pin in control.list_non_terminal_leases():
                if pin.operation_kind == "attachment" and (
                    not pin.operation_identity.startswith(("attkey_", "attref_"))
                    or self._upload_pin_owner(session_id, pin)[0] == thread_id
                ):
                    return ()
        finally:
            control.close()
        # 接受在线程池执行：必须先读 pin 再读队列，覆盖 pin → durable acceptance 的交接。
        if messages.execution_queue_snapshot(session_id, thread_id).records:
            return ()
        return self._catalog.release_associations(session_id=session_id, thread_id=thread_id,
                                                   binding_token=binding_token, now=self._clock())

    def reconcile_runtime_bindings(self, *, messages: AttachmentMessageReader,
                                   is_current: Callable[[str, str, str], bool]) -> tuple[str, ...]:
        """只核旧 runtime token；从未建图的冷上传/fork 关联仍由原流程持有。"""
        released = []
        for session_id, thread_id, token in self._catalog.runtime_bindings():
            if not is_current(session_id, thread_id, token):
                released.extend(self.release_runtime_binding(session_id, thread_id, token, messages=messages))
        return tuple(released)

    def _upload_pin_owner(self, session_id: str, pin: SessionOperationLease) -> tuple[str, str]:
        parts = (pin.recovery_ref or "").split(":")
        if not ((len(parts) == 4 and parts[0] == "attachment") or
                (len(parts) == 5 and parts[0] == "attachment-ref")) or parts[1] != session_id or not all(parts[2:]):
            raise RuntimeError("附件 pin 缺少精确消息身份，不能猜测恢复")
        return parts[2], parts[3]

    def _reference_pin_attachment(self, session_id: str, pin: SessionOperationLease,
                                  references: tuple[str, ...] | None) -> str:
        thread_id, message_id = self._upload_pin_owner(session_id, pin)
        attachment_id = pin.recovery_ref.split(":")[4]
        if references is not None:
            matches = [uri for uri in references if self._parse_canonical_attachment_id(session_id, uri, thread_id=thread_id) == attachment_id]
            if not any(hashlib.sha256(canonical_json_bytes([
                session_id, thread_id, message_id, pin.operation_identity, attachment_id, uri,
            ])).hexdigest() == pin.preimage_hash for uri in matches):
                raise RuntimeError("附件引用 pin 与已接受消息的冻结输入不一致")
            self._catalog.require_available_directory(attachment_id)
        return attachment_id

    def _validate_upload_pin(self, session_id: str, pin: SessionOperationLease, record: DirectoryIngestRecord | None) -> tuple[str, str]:
        thread_id, message_id = self._upload_pin_owner(session_id, pin)
        if record is not None:
            preimage = hashlib.sha256(canonical_json_bytes([
                session_id, thread_id, message_id, record.ingest_idempotency_key,
                record.digest, record.length, record.file_name, record.mime_type,
            ])).hexdigest()
            if (record.owner_session_id, record.owner_thread_id, record.message_id,
                record.pin_lease_id, record.pin_captured_generation, record.preimage_hash) != (
                session_id, thread_id, message_id, pin.lease_id, pin.captured_lifecycle_generation, pin.preimage_hash,
            ) or preimage != pin.preimage_hash or record.pin_fencing_token > pin.fencing_token:
                raise RuntimeError("附件 pin 与目录上传冻结输入不一致")
        return thread_id, message_id

    def complete_message_ingests(self, session_id: str, *, thread_id: str, message_id: str, messages: AttachmentMessageReader) -> None:
        control = self._open_control_store(session_id)
        try:
            pins = tuple(pin for pin in control.list_non_terminal_leases()
                         if pin.operation_kind == "attachment" and pin.operation_identity.startswith(("attkey_", "attref_"))
                         and self._upload_pin_owner(session_id, pin) == (thread_id, message_id))
            if not pins:
                return
            references = messages.attachment_message_references(session_id, thread_id, message_id)
            if references is None:
                raise RuntimeError("附件消息尚未被 canonical owner 接受")
            ids = {self._parse_canonical_attachment_id(session_id, uri, thread_id=thread_id) for uri in references}
            for pin in pins:
                if pin.operation_identity.startswith("attref_"):
                    self._reference_pin_attachment(session_id, pin, references)
                    self._settle_pin(control, pin, outcome="completed")
                    continue
                record = self._catalog.get_ingest_record(pin.operation_identity)
                self._validate_upload_pin(session_id, pin, record)
                if record.state != "published" or record.attachment_id not in ids:
                    raise RuntimeError("消息接受事实与已发布目录附件不一致")
                self._catalog.require_available_directory(record.attachment_id)
                observe_phase_sync(pin.operation_identity, "attachment.ingest.accepted", details={
                    "session_id": session_id, "thread_id": thread_id, "message_id": message_id,
                    "attachment_id": record.attachment_id, "lease_id": pin.lease_id,
                })
                self._settle_pin(control, pin, outcome="completed")
        finally:
            control.close()

    def recover_session(self, session_id: str, *, messages: AttachmentMessageReader) -> tuple[str, ...]:
        return self._recover_ingests(session_id, messages=messages)

    def _recover_ingests(self, session_id: str, *, messages: AttachmentMessageReader | None, thread_id: str | None = None) -> tuple[str, ...]:
        from app.services.infrastructure.attachment_blob_catalog.directories import (
            remove_directory,
        )

        actions = []
        control = self._open_control_store(session_id)
        try:
            pins = tuple(pin for pin in control.list_non_terminal_leases()
                         if pin.operation_kind == "attachment" and pin.operation_identity.startswith(("attkey_", "attref_"))
                         and (thread_id is None or self._upload_pin_owner(session_id, pin)[0] == thread_id))
            pending = tuple(record for record in self._catalog.list_non_terminal_ingest_records(owner_session_id=session_id)
                            if thread_id is None or record.owner_thread_id == thread_id)
            if {record.ingest_idempotency_key for record in pending} - {pin.operation_identity for pin in pins}:
                raise RuntimeError("非终态目录上传缺少有效 pin，不能猜测恢复")
            for pin in pins:
                if pin.operation_identity.startswith("attref_"):
                    owner_thread, message_id = self._upload_pin_owner(session_id, pin)
                    references = None if messages is None else messages.attachment_message_references(session_id, owner_thread, message_id)
                    self._reference_pin_attachment(session_id, pin, references)
                    if references is None and (messages is None or not messages.execution_queue_snapshot(session_id, owner_thread).records):
                        self._catalog.release_associations(session_id=session_id, thread_id=owner_thread,
                            binding_token=f"reference:{pin.operation_identity}", now=self._clock())
                        actions.append("released-unused-reference")
                    recovered = control.takeover_lease(lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token)
                    self._settle_pin(control, recovered, outcome="failed" if references is None else "completed")
                    actions.append("settled-pin")
                    continue
                try:
                    record = self._catalog.get_ingest_record(pin.operation_identity)
                except KeyError:
                    record = None
                owner_thread, message_id = self._validate_upload_pin(session_id, pin, record)
                outcome = "failed"
                if record is not None and record.state == "published" and messages is not None:
                    references = messages.attachment_message_references(session_id, owner_thread, message_id)
                    if references is not None:
                        if record.attachment_id not in {self._parse_canonical_attachment_id(session_id, uri, thread_id=owner_thread) for uri in references}:
                            raise RuntimeError("已接受消息缺少 pin 指定的目录附件")
                        self._catalog.require_available_directory(record.attachment_id)
                        outcome = "completed"
                    elif not messages.attachment_reference_in_use(session_id, owner_thread, record.attachment_id):
                        self._catalog.release_unused_ingest(record, now=self._clock())
                        actions.append("released-unused-reference")
                # aborted 只证明逻辑终止；目录删除失败后仍须按原 locator 重试，再结清 pin。
                if record is not None and record.state != "published":
                    self._catalog.abort_ingest_record(ingest_idempotency_key=record.ingest_idempotency_key,
                                                    reason="恢复：原上传 holder 已排空，未发布 intent 终止", now=self._clock())
                    remove_directory(resolve_staging_path(self._attachments_root, record.staging_relative_locator))
                    remove_directory(resolve_attachment_directory(self._attachments_root, record.relative_locator))
                    actions.append("aborted-ingest")
                recovered = control.takeover_lease(lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token)
                self._settle_pin(control, recovered, outcome=outcome)
                actions.append("settled-pin")
        finally:
            control.close()
        return tuple(actions)

    async def recover_idle_session(self, session_id: str, *, messages: AttachmentMessageReader) -> tuple[str, ...]:
        """恢复一个已发布 Session；不抢占活上传，始终遵循 holder → gate 锁序。"""
        async with self.deletion_holder.lock_for(session_id, wait=False) as holder:
            if not holder.acquired:
                return ()
            async with self._gate.exclusive(session_id):
                # 删除 journal 接管原 pin；成功 fork 回执不再解析已隔离的节点。
                catalog = self._path_resolver.catalog_store
                if catalog.find_operation_replay_delete(self._path_resolver.workspace_id, session_id) is not None:
                    return ()
                # 没有删除原件的缺失仍须报错，不能把外部损坏当作已删除。
                if catalog.get_node(session_id).state != "active":
                    return ()
                return self.recover_session(session_id, messages=messages)

    async def recover_pending_ingests(self, *, messages: AttachmentMessageReader) -> int:
        """启动时以权威 Session catalog 枚举，复用同一定点恢复入口。"""
        _, nodes = self._path_resolver.catalog_store.read_nodes_snapshot()
        count = 0
        for node in nodes:
            if node.kind == "session" and node.state == "active":
                count += (await self.recover_idle_session(node.node_id, messages=messages)).count("settled-pin")
        return count

    def release_session_references(self, session_id: str) -> tuple[str, ...]:
        """删除 drain：释放该 session 的 owner reference 并收敛其 ingest（定点）。"""
        self._release_copy_attachment_claims(session_id)
        released = self._catalog.release_associations(session_id=session_id, now=self._clock())
        actions = self._recover_ingests(session_id, messages=None)
        return released + actions

    def release_thread_references(self, session_id: str, thread_id: str) -> None:
        """Thread 删除持有目标 Session gate 时释放精确引用，不释放复用的 main 引用。"""
        self._release_copy_attachment_claims(session_id, thread_id=thread_id)
        self._catalog.release_associations(session_id=session_id, thread_id=thread_id, now=self._clock())
        self._recover_ingests(session_id, messages=None, thread_id=thread_id)

    def _release_copy_attachment_claims(self, session_id: str, *, thread_id: str | None = None) -> None:
        control = self._open_control_store(session_id)
        try:
            records = tuple(record for record in read_copy_attachment_settlements(control)
                            if thread_id is None or record.target_thread_id == thread_id)
            operations = {record.copy_operation_id: record.claim_ids for record in records if record.state != "aborted"}
            for operation in self._catalog.fork_attachment_operations_for_target(session_id, target_thread_id=thread_id):
                operations.setdefault(operation, self._catalog.fork_attachment_claim_ids(operation))
            for operation, claim_ids in operations.items():
                self._catalog.settle_fork_attachment_claims(copy_operation_id=operation, claim_ids=claim_ids, state="target_deleted")
            for record in records:
                if record.state not in {"aborted", "target_deleted"}:
                    transition_copy_attachment_settlement(control, record, state="target_deleted")
        finally:
            control.close()

    def _settle_pin(self, control: SessionControlStore, pin: SessionOperationLease, *, outcome: str) -> None:
        """只结算当前 token；已完成可幂等确认，冲突与数据库故障必须暴露。"""
        current = control.get_lease(pin.lease_id)
        if current.fencing_token != pin.fencing_token:
            raise RuntimeError(f"附件 pin 结算 token 已失效: lease_id={pin.lease_id}")
        if current.state == outcome:
            return
        if current.state == "active":
            current = control.mark_lease_settling(
                lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token
            )
        control.settle_lease(
            lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token, outcome=outcome,
        )
