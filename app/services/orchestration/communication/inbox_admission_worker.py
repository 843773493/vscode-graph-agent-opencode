"""InboxAdmissionWorker：target_accepted 未绑定 inbox 的唯一恢复 worker。

对应 design.md §600：acceptance 提交事件与 backend startup 时，从持久
状态索引（communication_inbox.state='target_accepted'）恢复未绑定
inbox，按 admission_id/wakeup key 幂等 admit。本 worker：

- 只消费 SessionControlStore 的状态索引（不扫目录、不读 thread.json、
  不依赖内存 future）；
- claim 由 store 单事务闸门保证（相同 claim 幂等重入，不同 claim 冲突
  fail closed，同 owner 更高 generation 接管）；
- binder 是可注入 typed port；binder 成功返回的 job/turn binding 经
  mark_communication_inbox_execution_bound CAS 提交（已 bound 且相同
  identity 幂等）；
- binder 异常或 identity 非法经 record failure 记录后保持
  target_accepted 可恢复，本轮结束显式抛 InboxAdmissionError，绝不
  静默吞掉。

生产 binder 经 RuntimeSourceDispatcher 的唯一 source/wakeup/admission 链路建立 binding；
本模块只领取与 CAS，不提供默认 binder。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.phase_observer import observe_phase
from app.core.session_control_primitives import validate_claim_fields
from app.core.session_control_store import (
    CommunicationInboxRecord,
    SessionControlStore,
)
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    SessionOperationLease,
)
from app.services.business.communication.ledger import SessionLeaseSettlement

__all__ = [
    "InboxAdmissionAttempt",
    "InboxAdmissionBinder",
    "InboxAdmissionError",
    "InboxAdmissionTarget",
    "InboxAdmissionWorker",
    "InboxExecutionBinding",
]


@dataclass(frozen=True, slots=True)
class InboxAdmissionTarget:
    """binder 输入：冻结 inbox 投影（含确定性 admission_id/wakeup_key）。

    binder 不得读取磁盘或闭包补身份；真实 binder 以 admission_id +
    payload_hash create-or-get Job 后返回实际绑定 identity。
    """

    inbox: CommunicationInboxRecord


@dataclass(frozen=True, slots=True)
class InboxExecutionBinding:
    """binder 成功返回：实际建立的 job/turn 绑定。"""

    job_id: str
    execution_id: str
    turn_id: str | None


class InboxAdmissionBinder(Protocol):
    """可注入 binder 协议：为一个冻结 inbox 建立 execution binding。

    失败直接抛异常；worker 负责记录 last_error 并显式暴露。
    """

    async def __call__(self, target: InboxAdmissionTarget) -> InboxExecutionBinding: ...


@dataclass(frozen=True, slots=True)
class InboxAdmissionAttempt:
    """单条 inbox 的单次消费结果。

    outcome 闭集：bound（本轮完成绑定）、skipped_claim_held（claim 被其他
    worker 持有——并发契约的正常输家）、skipped_already_bound（列出后已被
    其他 worker 绑定）、cancelled（删除收敛原 inbox）、error（失败记录并由上层
    显式抛出异常）。error 字段仅在失败路径非空。
    """

    communication_id: str
    outcome: str
    bound_inbox: CommunicationInboxRecord | None = None
    error: str | None = None


class InboxAdmissionError(RuntimeError):
    """单轮消费存在显式失败（binder 异常 / identity 非法 / store 拒绝）。

    失败明细已逐条写入 inbox last_error；本异常是显式暴露通道。
    """

    def __init__(self, failures: Sequence[str]) -> None:
        super().__init__(
            "inbox admission 单轮消费存在显式失败（已记录 last_error，"
            f"不静默吞掉）: {'; '.join(failures)}"
        )


class InboxAdmissionWorker:
    """单一 target_accepted inbox 恢复 worker（显式生命周期）。

    - start()：显式启动，binder 必须由组合根注入。
    - stop()：显式关闭；幂等 no-op。
    - admit_pending_once()：单轮消费全部 target_accepted inbox。
    """

    def __init__(
        self,
        *,
        store: SessionControlStore,
        session_id: str,
        lease_settlement: SessionLeaseSettlement,
        claim_owner: str,
        binder: InboxAdmissionBinder,
        claim_generation: int = 1,
    ) -> None:
        if not isinstance(store, SessionControlStore):
            raise TypeError(f"store 必须是 SessionControlStore: {store!r}")
        validate_claim_fields(claim_owner, claim_generation)
        if not callable(binder):
            raise TypeError("InboxAdmissionWorker 必须注入可调用的 binder")
        self._store = store
        self._session_id = session_id
        self._lease_settlement = lease_settlement
        self._claim_owner = claim_owner
        self._claim_generation = claim_generation
        self._binder = binder
        self._started = False

    def start(self) -> None:
        """显式启动；重复启动直接报告生命周期错误。"""
        if self._started:
            raise RuntimeError("InboxAdmissionWorker 已启动")
        self._started = True

    def stop(self) -> None:
        """显式关闭；重复关闭是幂等 no-op。"""
        self._started = False

    async def admit_pending_once(self) -> tuple[InboxAdmissionAttempt, ...]:
        """单轮消费全部 target_accepted inbox；失败在轮末显式抛出。

        逐条流程：claim（冲突按并发契约分类跳过）→ binder →
        mark_execution_bound CAS。binder 异常 / identity 非法先写
        last_error（inbox 保持 target_accepted 可恢复），全部处理完后
        抛 InboxAdmissionError 汇总显式暴露。
        """
        if not self._started:
            raise RuntimeError("InboxAdmissionWorker 未启动（必须先 start()）")
        binder = self._binder
        attempts: list[InboxAdmissionAttempt] = []
        failures: list[str] = []
        for inbox in self._store.list_target_accepted_communication_inboxes():
            attempt = await self._consume_one(inbox, binder)
            attempts.append(attempt)
            if attempt.error is not None:
                failures.append(f"{inbox.communication_id}: {attempt.error}")
        if failures:
            raise InboxAdmissionError(failures)
        return tuple(attempts)

    async def _consume_one(
        self,
        inbox: CommunicationInboxRecord,
        binder: InboxAdmissionBinder,
    ) -> InboxAdmissionAttempt:
        """消费单条 inbox；claim 冲突按并发契约分类，binder 失败记录。"""
        communication_id = inbox.communication_id
        lease = self._store.find_lease_by_operation("communication-target|" + communication_id)
        if lease is None or lease.operation_kind != "communication_target" or lease.preimage_hash != inbox.payload_hash:
            raise RuntimeError("通信 inbox 缺少同 identity/hash 的原 lifecycle lease")
        try:
            async with self._lease_settlement(self._session_id, self._store, lease) as active:
                if not active:
                    return self._cancel_inbox(inbox, lease)
                self._store.claim_communication_inbox_admission(
                    communication_id,
                    claim_owner=self._claim_owner,
                    claim_generation=self._claim_generation,
                )
        except RuntimeError:
            current = self._store.get_communication_inbox(communication_id)
            if current.state == "execution_bound":
                return InboxAdmissionAttempt(
                    communication_id=communication_id,
                    outcome="skipped_already_bound",
                )
            if (
                current.state == "target_accepted"
                and current.admission_claim_owner is not None
                and current.admission_claim_owner != self._claim_owner
            ):
                return InboxAdmissionAttempt(
                    communication_id=communication_id,
                    outcome="skipped_claim_held",
                )
            raise
        try:
            await observe_phase(communication_id, "communication.inbox.claimed")
            binding = await binder(InboxAdmissionTarget(inbox=inbox))
            if not isinstance(binding, InboxExecutionBinding):
                raise TypeError(f"binder 返回类型非法: {type(binding).__name__}")
            await observe_phase(communication_id, "communication.inbox.job_created")
            async with self._lease_settlement(self._session_id, self._store, lease):
                bound = self._store.mark_communication_inbox_execution_bound(
                    communication_id,
                    job_id=binding.job_id,
                    execution_id=binding.execution_id,
                    turn_id=binding.turn_id,
                    claim_owner=self._claim_owner,
                    claim_generation=self._claim_generation,
                )
                current_lease = self._store.assert_lease_current(lease)
                if current_lease.state == "settling":
                    self._store.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="completed")
        except SessionDeletionPendingError:
            # fresh binder 的 active 门拒绝迟到 Job；原 inbox 必须在原 gate
            # 明确落下取消事实，再把 holder 交给删除 owner。
            async with self._lease_settlement(self._session_id, self._store, lease) as active:
                if active:
                    raise
                return self._cancel_inbox(inbox, lease)
        except asyncio.CancelledError:
            await self._record_failure(communication_id, lease, "inbox worker 已停止，原 admission 保留待恢复")
            raise
        except Exception as error:  # noqa: BLE001 —— 记录后显式暴露
            return await self._record_failure(communication_id, lease, f"{type(error).__name__}: {error}")
        return InboxAdmissionAttempt(
            communication_id=communication_id,
            outcome="bound",
            bound_inbox=bound,
        )

    def _cancel_inbox(
        self, inbox: CommunicationInboxRecord, lease: SessionOperationLease,
    ) -> InboxAdmissionAttempt:
        """调用方持原 lease 结算 gate；只取消这一条原通信，不建立新 binding。"""
        self._store.drain_communication_inboxes(communication_id=inbox.communication_id)
        current = self._store.assert_lease_current(lease)
        if current.state == "settling":
            self._store.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="cancelled")
        return InboxAdmissionAttempt(communication_id=inbox.communication_id, outcome="cancelled", bound_inbox=self._store.get_communication_inbox(inbox.communication_id))

    async def _record_failure(
        self,
        communication_id: str,
        lease: SessionOperationLease,
        message: str,
    ) -> InboxAdmissionAttempt:
        """记录 last_error 并保持 target_accepted 可恢复；返回失败 attempt。"""
        try:
            async with self._lease_settlement(self._session_id, self._store, lease):
                self._store.record_communication_inbox_admission_failure(
                    communication_id,
                    claim_owner=self._claim_owner,
                    claim_generation=self._claim_generation,
                    last_error=message,
                )
        except (RuntimeError, KeyError) as record_error:
            message = f"{message}（record failure 本身失败: {record_error}）"
        return InboxAdmissionAttempt(
            communication_id=communication_id,
            outcome="error",
            error=message,
        )
