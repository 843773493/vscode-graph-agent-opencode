"""Saver 的内部执行控制准入与恢复；接受事实归 rollout execution owner。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import NavigationTopologyGate, SessionLifecycleGate
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionInterrupt,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.services.infrastructure.rollout_context.checkpoint.mutation.saver_mutation_intents import (
    source_lifecycle_item,
)
from app.services.infrastructure.rollout_context.execution.internal_control import (
    InternalInterruptEvent,
)


class InternalExecutionControlOwnerMixin:
    def internal_execution_interrupt(self, admission: ExecutionAdmission) -> InternalExecutionInterrupt | None:
        return self._storage.executions.internal_execution_interrupt(admission)

    @staticmethod
    def _settle_internal_interrupt_lease(control: SessionControlStore, event: InternalInterruptEvent) -> None:
        request = event.request
        lease = control.get_lease(event.lease_id)
        if (lease.operation_kind, lease.operation_identity, lease.preimage_hash, lease.recovery_ref) != (
            "context_control", f"internal-interrupt:{request.interrupt_request_id}",
            event.lease_preimage_hash, request.admission.execution_id,
        ):
            raise RuntimeError("内部中断事件与 lease identity/hash 不一致")
        if lease.state == "active":
            lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
        if lease.state == "settling":
            control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="completed")
        elif lease.state != "completed":
            raise RuntimeError("已接受内部中断的 lease 不得失败或撤回")

    def request_internal_execution_interrupt(
        self, request: InternalExecutionInterrupt, decision: ApplySourceLifecycleDecision,
    ) -> InternalExecutionInterrupt | str:
        """同一写锁裁决自然终结；提醒和接受事件在同一 rollout 事务提交。"""
        admission = request.admission
        owner = decision.owner
        if ((owner.session_id, owner.thread_id, decision.execution_id, decision.item_id)
                != (admission.session_id, admission.thread_id, admission.execution_id, request.reminder_item_id)
                or decision.decision_kind != "observe_pending" or decision.content is None
                or not decision.pending_only or decision.source_kind != "checkpoint_reminder"
                or decision.name != "user_interrupt"
                or decision.source_id != f"checkpoint:user_interrupt:{request.interrupt_request_id}"):
            raise ValueError("内部中断提醒与精确 execution owner 不一致")
        storage, executions = self._storage.storage, self._storage.executions
        item = source_lifecycle_item(decision)
        with storage._lock(admission.thread_id, "", session_id=admission.session_id):
            actual = executions.execution_admission(
                admission.session_id, thread_id=admission.thread_id, execution_id=admission.execution_id,
            )
            if actual != admission:
                raise ValueError("内部中断目标与持久 admission 不一致")
            with storage.read_connection(admission.session_id, admission.thread_id, "") as connection:
                collision = connection.execute(
                    "SELECT entity_id FROM control_events WHERE control_kind='interrupt.requested' "
                    "AND entity_type='internal_execution' "
                    "AND json_extract(payload_json,'$.request.interrupt_request_id')=?",
                    (request.interrupt_request_id,),
                ).fetchall()
                if collision and collision != [(admission.execution_id,)]:
                    raise ValueError("内部中断 command identity 已属于其它 execution")
                event = executions._internal_interrupt_event(connection, admission)
                outcome = executions.runtime_execution_outcome(admission.session_id, admission.thread_id, admission.execution_id)
            with closing(SessionControlStore(storage._path_resolver.resolve_session_node(admission.session_id) / CONTROL_DATABASE_NAME)) as control:
                if event is not None:
                    if event.request.interrupt_request_id == request.interrupt_request_id:
                        if event.request != request:
                            raise ValueError("内部中断 command identity 内容冲突")
                        # 同命令重试仍核验完整正文，不以已接受状态掩盖不同内容。
                        with executions._execution_input_transaction(admission.session_id, admission.thread_id) as connection:
                            self._storage.checkpoints._apply_source_lifecycle_decision_transaction(connection, decision, item)
                    self._settle_internal_interrupt_lease(control, event)
                    return event.request
                if outcome != "unknown":
                    return outcome
                preimage_hash = sha256_jcs({"request": asdict(request), "decision": asdict(decision)}).rsplit(":", 1)[1]
                # 不同时持有两库写事务；OS operation lock 覆盖检查、lease 和接受。
                lease = control.create_or_get_lease(
                    operation_kind="context_control", operation_identity=f"internal-interrupt:{request.interrupt_request_id}",
                    preimage_hash=preimage_hash, recovery_ref=admission.execution_id,
                )
                if lease.state == "active":
                    lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                if lease.state != "settling":
                    raise RuntimeError("内部中断接受 lease 已终结")
                with executions._execution_input_transaction(admission.session_id, admission.thread_id) as connection:
                    # BEGIN IMMEDIATE 后重核终态；接受点只在本事务提交后成立。
                    if connection.execute("SELECT outcome FROM executions WHERE execution_id=?", (admission.execution_id,)).fetchone() != ("unknown",):
                        raise RuntimeError("内部中断接受期间 execution outcome 改变")
                    self._storage.checkpoints._apply_source_lifecycle_decision_transaction(connection, decision, item)
                    event = InternalInterruptEvent(request, lease.lease_id, preimage_hash)
                    executions._append_internal_interrupt(connection, event)
                self._settle_internal_interrupt_lease(control, event)
                return request

    async def recover_internal_interrupts(self) -> int:
        """启动时收敛未确认停止的请求；不重放模型，也不改写 fork 历史。"""
        storage = self._storage.storage
        sessions = {owner.session_id for owner in self._storage.checkpoints.list_checkpoint_owners()}
        count = 0
        for session_id in sessions:
            async with (NavigationTopologyGate(storage.sessions_dir).shared(), SessionLifecycleGate(storage.sessions_dir).exclusive(session_id)):
                if storage._path_resolver.get_node(session_id).state != "active":
                    continue
                accepted_leases = set()
                with closing(SessionControlStore(storage._path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME)) as control:
                    for owner in self._storage.checkpoints.list_checkpoint_owners(session_id=session_id):
                        for event in self._storage.executions.local_internal_interrupts(session_id, owner.thread_id):
                            request = event.request
                            accepted_leases.add(event.lease_id)
                            outcome = self.runtime_execution_outcome(session_id, owner.thread_id, request.admission.execution_id)
                            if outcome == "unknown":
                                self.mark_execution_lost(session_id, thread_id=owner.thread_id, turn_id=None,
                                                         execution_id=request.admission.execution_id, reason="process_exit_after_interrupt")
                                count += 1
                            elif outcome not in {"failed", "cancelled", "interrupted", "execution_lost"}:
                                raise RuntimeError("内部中断接受后出现冲突 execution 终态")
                            self._settle_internal_interrupt_lease(control, event)
                    # lease 先提交、rollout 接受未提交：没有请求可重放，明确失败。
                    for lease in control.list_non_terminal_leases():
                        if lease.operation_identity.startswith("internal-interrupt:") and lease.lease_id not in accepted_leases:
                            lease = control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                            lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                            control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="failed")
        return count
