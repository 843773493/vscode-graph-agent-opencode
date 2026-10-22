"""附件消息 pin 的接受结算、崩溃恢复与删除收敛；不建立第二份消息事实。"""

from __future__ import annotations

import hashlib
from typing import Protocol

from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
    transition_copy_attachment_settlement,
)
from app.core.session_lifecycle_gate import SessionOperationLease
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentIngestRecord,
    derive_attachment_id,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_staging_path,
)


class AttachmentMessageReader(Protocol):
    """canonical owner 提供接受与使用事实；附件 store 不读取 rollout 数据库。"""

    def attachment_message_references(self, session_id: str, thread_id: str, message_id: str) -> tuple[str, ...] | None: ...

    def attachment_reference_in_use(self, session_id: str, thread_id: str, attachment_id: str) -> bool: ...


class AttachmentIngestRecoveryMixin:
    """宿主提供 catalog、locator、Session gate 与控制库端口。"""

    def _upload_pin_owner(self, session_id: str, pin: SessionOperationLease) -> tuple[str, str]:
        parts = (pin.recovery_ref or "").split(":")
        if len(parts) != 4 or parts[:2] != ["attachment", session_id] or not all(parts[2:]):
            raise RuntimeError(f"附件 pin 缺少冻结的消息身份，拒绝猜测恢复: lease_id={pin.lease_id}")
        return parts[2], parts[3]

    def _validate_upload_pin(
        self, session_id: str, pin: SessionOperationLease, record: AttachmentIngestRecord | None,
    ) -> tuple[str, str]:
        thread_id, message_id = self._upload_pin_owner(session_id, pin)
        if record is not None and (
            record.owner_session_id != session_id or record.owner_thread_id != thread_id
            or record.pin_lease_id != pin.lease_id or record.preimage_hash != pin.preimage_hash
            or record.pin_captured_generation != pin.captured_lifecycle_generation
            or record.pin_fencing_token > pin.fencing_token
            or (record.digest is not None and record.preimage_hash != hashlib.sha256(
                f"{session_id}|{thread_id}|{message_id}|{record.digest}|{record.length}".encode()
            ).hexdigest())
        ):
            raise RuntimeError(f"附件 pin 与 ingest 冻结身份不一致: lease_id={pin.lease_id}")
        return thread_id, message_id

    def complete_message_ingests(
        self, session_id: str, *, thread_id: str, message_id: str, messages: AttachmentMessageReader,
    ) -> None:
        """正常接受路径只结算本消息 pin；先核 canonical 提交，再 CAS 终态。

        产品调用方在消息准备 holder 内执行。这里不发布或释放引用，不取得第二把
        gate；崩溃恢复必须先排空 holder，才能接管令牌和释放未使用引用。
        """
        control = self._open_control_store(session_id)
        try:
            pins = tuple(pin for pin in control.list_non_terminal_leases()
                         if pin.operation_kind == "attachment" and pin.operation_identity.startswith("attkey_")
                         and pin.recovery_ref == f"attachment:{session_id}:{thread_id}:{message_id}")
            if not pins:
                return
            references = messages.attachment_message_references(session_id, thread_id, message_id)
            if references is None:
                raise RuntimeError(f"附件消息尚未被 canonical owner 接受: message_id={message_id}")
            attachment_ids = {self._parse_logical_file_id(session_id, uri) for uri in references}
            for pin in pins:
                record = self._catalog.get_ingest_record(pin.operation_identity)
                self._validate_upload_pin(session_id, pin, record)
                if record.state != "published" or derive_attachment_id(
                    blob_id=record.blob_id, owner_session_id=session_id, owner_thread_id=thread_id,
                ) not in attachment_ids:
                    raise RuntimeError(f"消息接受事实与已发布附件不一致: lease_id={pin.lease_id}")
                self._settle_pin(control, pin, outcome="completed")
        finally:
            control.close()

    def recover_session(self, session_id: str, *, messages: AttachmentMessageReader) -> tuple[str, ...]:
        """holder 已退出且持有 Session gate 时，按 canonical 使用事实恢复。"""
        return self._recover_ingests(session_id, messages=messages)

    def _recover_ingests(
        self, session_id: str, *, messages: AttachmentMessageReader | None, thread_id: str | None = None,
    ) -> tuple[str, ...]:
        """messages=None 仅由已释放引用的删除 owner 调用；不把发布当作接受。"""
        actions: list[str] = []
        control = self._open_control_store(session_id)
        try:
            # copy 附件 lease 由唯一 fork settlement owner 处理，不能混入上传。
            pins = tuple(pin for pin in control.list_non_terminal_leases()
                         if pin.operation_kind == "attachment" and pin.operation_identity.startswith("attkey_")
                         and (thread_id is None or self._upload_pin_owner(session_id, pin)[0] == thread_id))
            pending = tuple(record for record in self._catalog.list_non_terminal_ingest_records(owner_session_id=session_id)
                            if thread_id is None or record.owner_thread_id == thread_id)
            if {record.ingest_idempotency_key for record in pending} - {pin.operation_identity for pin in pins}:
                raise RuntimeError(f"非终态附件 ingest 缺少对应有效 pin: session_id={session_id}")
            for pin in pins:
                try:
                    record = self._catalog.get_ingest_record(pin.operation_identity)
                except KeyError:
                    record = None
                owner_thread, message_id = self._validate_upload_pin(session_id, pin, record)
                outcome = "failed"
                if record is not None and record.state == "published" and messages is not None:
                    attachment_id = derive_attachment_id(
                        blob_id=record.blob_id, owner_session_id=session_id, owner_thread_id=owner_thread,
                    )
                    ref = self._catalog.get_owner_ref(attachment_id)
                    if ref is None or (ref.owner_session_id, ref.owner_thread_id, ref.blob_id, ref.digest) != (
                        session_id, owner_thread, record.blob_id, record.digest,
                    ):
                        raise RuntimeError(f"已发布 ingest 缺少一致的 owner reference: lease_id={pin.lease_id}")
                    references = messages.attachment_message_references(session_id, owner_thread, message_id)
                    if references is not None:
                        if ref.state != "active" or attachment_id not in {self._parse_logical_file_id(session_id, uri) for uri in references}:
                            raise RuntimeError(f"消息接受事实缺少 pin 指定附件: lease_id={pin.lease_id}")
                        outcome = "completed"
                    elif not messages.attachment_reference_in_use(session_id, owner_thread, attachment_id):
                        if self._catalog.release_unused_owner_ref(
                            attachment_id=attachment_id, owner_session_id=session_id, owner_thread_id=owner_thread,
                        ):
                            actions.append("released-unused-reference")
                        elif ref.state == "active":
                            # fork 仍在准备，尚不能把其保活当成新的 canonical 使用。
                            # 保留原 pin，待 fork 发布或明确撤销后再判断，避免失去恢复入口。
                            actions.append("retained-pin")
                            continue
                if record is not None and record.state not in {"published", "aborted"}:
                    resolve_staging_path(self._attachments_root, record.staging_relative_locator).unlink(missing_ok=True)
                    self._catalog.abort_ingest_record(
                        ingest_idempotency_key=record.ingest_idempotency_key,
                        reason="恢复：上传 holder 已停止，未发布 ingest 明确终止",
                    )
                    actions.append("aborted-ingest")
                recovered_pin = control.takeover_lease(
                    lease_id=pin.lease_id, expected_fencing_token=pin.fencing_token,
                )
                self._settle_pin(control, recovered_pin, outcome=outcome)
                actions.append("settled-pin")
        finally:
            control.close()
        # TODO: 按唯一 claim 协调未发布正文的回收与同 digest 后续上传，不能扫盘或误删其它 owner。
        return tuple(actions)

    async def recover_idle_session(self, session_id: str, *, messages: AttachmentMessageReader) -> tuple[str, ...]:
        """恢复一个已发布 Session；不抢占活上传，始终遵循 holder → gate 锁序。"""
        async with self.deletion_holder.lock_for(session_id, wait=False) as holder:
            if not holder.acquired:
                return ()
            async with self._gate.exclusive(session_id):
                # 等 gate 期间删除可能已关闭准入，由删除 owner 收敛其 pin。
                if self._path_resolver.catalog_store.get_node(session_id).state != "active":
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
        released = self._catalog.release_owner_refs_for_session(
            owner_session_id=session_id, reason="owner session 删除"
        )
        actions = self._recover_ingests(session_id, messages=None)
        return tuple(ref.attachment_id for ref in released) + actions

    def release_thread_references(self, session_id: str, thread_id: str) -> None:
        """Thread 删除持有目标 Session gate 时释放精确引用，不释放复用的 main 引用。"""
        self._release_copy_attachment_claims(session_id, thread_id=thread_id)
        self._catalog.release_owner_refs_for_session(
            owner_session_id=session_id, owner_thread_id=thread_id, reason="owner thread 删除",
        )
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
