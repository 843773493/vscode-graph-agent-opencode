"""内部执行中断的 typed 控制事实；不把历史来源事件当作目标控制。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, dataclass

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionInterrupt,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)


@dataclass(frozen=True, slots=True)
class InternalInterruptEvent:
    """rollout 接受事实与跨库交付 lease 的不可变关联。"""

    request: InternalExecutionInterrupt
    lease_id: str
    lease_preimage_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise ValueError("内部中断事件缺少 lease identity")
        if not isinstance(self.lease_preimage_hash, str) or len(self.lease_preimage_hash) != 64:
            raise ValueError("内部中断事件缺少 lease preimage hash")


class InternalExecutionControlMixin:
    def _internal_interrupt_event(
        self, connection: sqlite3.Connection, admission: ExecutionAdmission,
    ) -> InternalInterruptEvent | None:
        rows = connection.execute(
            "SELECT payload_json FROM control_events WHERE control_kind='interrupt.requested' "
            "AND entity_type='internal_execution' AND entity_id=?",
            (admission.execution_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError(f"内部 execution 存在多个接受中断事实: {admission.execution_id}")
        payload = json.loads(rows[0][0])
        if not isinstance(payload, dict) or set(payload) != {"request", "lease_id", "lease_preimage_hash"}:
            raise ValueError("内部中断 control event payload 非法")
        raw = payload["request"]
        if not isinstance(raw, dict):
            raise TypeError("内部中断 request 必须是对象")
        request = InternalExecutionInterrupt(
            **{**raw, "admission": ExecutionAdmission(**raw["admission"])},
        )
        if request.admission != admission:
            raise RuntimeError("内部中断事件与 execution admission owner 不一致")
        reminder = connection.execute(
            "SELECT execution_id,semantic_kind FROM item_catalog WHERE item_id=?",
            (request.reminder_item_id,),
        ).fetchone()
        if reminder != (admission.execution_id, "runtime_notice"):
            raise RuntimeError("内部中断事件缺少同 owner 的 canonical reminder")
        return InternalInterruptEvent(request, payload["lease_id"], payload["lease_preimage_hash"])

    def internal_execution_interrupt(
        self, admission: ExecutionAdmission, *, connection: sqlite3.Connection | None = None,
    ) -> InternalExecutionInterrupt | None:
        if admission.execution_origin != "internal":
            raise ValueError("internal interrupt reader 只接受 internal admission")
        with (nullcontext(connection) if connection is not None else self._storage.read_connection(
            admission.session_id, admission.thread_id, "",
        )) as reader:
            actual = self.execution_admission(
                admission.session_id, thread_id=admission.thread_id,
                execution_id=admission.execution_id, writer_connection=reader,
            )
            if actual != admission:
                raise ValueError("内部中断读取与精确 admission 不一致")
            event = self._internal_interrupt_event(reader, admission)
            return event.request if event is not None else None

    def _append_internal_interrupt(
        self, connection: sqlite3.Connection, event: InternalInterruptEvent,
    ) -> None:
        """调用方已在同事务提交提醒；此处只发布唯一接受事实。"""
        if not connection.in_transaction:
            raise RuntimeError("内部中断接受必须在 rollout 写事务内")
        request = event.request
        timestamp = utc_now_text()
        sequence = self._storage._insert_control(
            connection, "interrupt.requested", "internal_execution",
            request.admission.execution_id, None, None, None,
            asdict(event),
            f"internal-interrupt:{request.interrupt_request_id}", timestamp,
        )
        connection.execute(
            "UPDATE database_meta SET last_control_sequence=?,updated_at=? WHERE singleton_id=1",
            (sequence, timestamp),
        )

    def local_internal_interrupts(
        self, session_id: str, thread_id: str,
    ) -> tuple[InternalInterruptEvent, ...]:
        """恢复只处理本物理 owner；fork 继承日志保留原来源身份且不可操作。"""
        try:
            with self._storage.read_connection(session_id, thread_id, "") as connection:
                rows = connection.execute(
                    "SELECT e.execution_id FROM executions e JOIN control_events c "
                    "ON c.entity_id=e.execution_id AND c.entity_type='internal_execution' "
                    "AND c.control_kind='interrupt.requested' "
                    "WHERE e.session_id=? AND e.thread_id=? "
                    "AND e.execution_origin='internal' AND e.turn_id IS NULL",
                    (session_id, thread_id),
                ).fetchall()
                events = []
                for (execution_id,) in rows:
                    admission = self.execution_admission(
                        session_id, thread_id=thread_id, execution_id=execution_id,
                        writer_connection=connection,
                    )
                    event = self._internal_interrupt_event(connection, admission)
                    if event is None:
                        raise RuntimeError("恢复内部中断时控制事件消失")
                    events.append(event)
                return tuple(events)
        except RolloutNotMaterializedError:
            return ()
