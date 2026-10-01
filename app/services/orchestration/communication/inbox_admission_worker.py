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

真实 binder（JobService create-or-get by admission_id + preimage hash）
属后续 send tool 接线切片；本模块不提供默认 binder，也不注册任何会
启动真实 Job 的实现。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.session_control_store import (
    CommunicationInboxRecord,
    SessionControlStore,
)

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
    turn_id: str | None


class InboxAdmissionBinder(Protocol):
    """可注入 binder 协议：为一个冻结 inbox 建立 execution binding。

    失败直接抛异常；worker 负责记录 last_error 并显式暴露。
    """

    def __call__(self, target: InboxAdmissionTarget) -> InboxExecutionBinding: ...


@dataclass(frozen=True, slots=True)
class InboxAdmissionAttempt:
    """单条 inbox 的单次消费结果。

    outcome 闭集：bound（本轮完成绑定）、skipped_claim_held（claim 被其他
    worker 持有——并发契约的正常输家）、skipped_already_bound（列出后已被
    其他 worker 绑定）。error 仅在 binder 失败路径非空。
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

    - start()：显式启动；缺 binder 明确报告 unavailable，不消费任何
      inbox、不把任何 inbox 标成 bound。
    - stop()：显式关闭；幂等 no-op。
    - admit_pending_once()：单轮消费全部 target_accepted inbox。
    """

    def __init__(
        self,
        *,
        store: SessionControlStore,
        claim_owner: str,
        claim_generation: int = 1,
        binder: InboxAdmissionBinder | None = None,
    ) -> None:
        if not isinstance(store, SessionControlStore):
            raise TypeError(f"store 必须是 SessionControlStore: {store!r}")
        if not isinstance(claim_owner, str) or not claim_owner:
            raise ValueError(f"claim_owner 不能为空: {claim_owner!r}")
        if (
            isinstance(claim_generation, bool)
            or not isinstance(claim_generation, int)
            or claim_generation < 1
        ):
            raise ValueError(
                f"claim_generation 必须是 >= 1 的整数: {claim_generation!r}"
            )
        self._store = store
        self._claim_owner = claim_owner
        self._claim_generation = claim_generation
        self._binder = binder
        self._started = False

    @property
    def binder_available(self) -> bool:
        """是否已注入 binder（缺省 None：真实 Job binder 接线前的形态）。"""
        return self._binder is not None

    def start(self) -> None:
        """显式启动；缺 binder 明确报告 unavailable，不产生任何状态变更。"""
        if self._binder is None:
            raise RuntimeError(
                "InboxAdmissionWorker 缺少 binder，启动 unavailable（真实 "
                "Job binder 属后续 send tool 接线切片）；未把任何 inbox "
                "标成 bound"
            )
        if self._started:
            raise RuntimeError("InboxAdmissionWorker 已启动")
        self._started = True

    def stop(self) -> None:
        """显式关闭；重复关闭是幂等 no-op。"""
        self._started = False

    def admit_pending_once(self) -> tuple[InboxAdmissionAttempt, ...]:
        """单轮消费全部 target_accepted inbox；失败在轮末显式抛出。

        逐条流程：claim（冲突按并发契约分类跳过）→ binder →
        mark_execution_bound CAS。binder 异常 / identity 非法先写
        last_error（inbox 保持 target_accepted 可恢复），全部处理完后
        抛 InboxAdmissionError 汇总显式暴露。
        """
        if not self._started:
            raise RuntimeError("InboxAdmissionWorker 未启动（必须先 start()）")
        if self._binder is None:
            raise RuntimeError(
                "InboxAdmissionWorker 缺少 binder，unavailable：不消费任何 inbox"
            )
        binder = self._binder
        attempts: list[InboxAdmissionAttempt] = []
        failures: list[str] = []
        for inbox in self._store.list_target_accepted_communication_inboxes():
            attempt = self._consume_one(inbox, binder)
            attempts.append(attempt)
            if attempt.error is not None:
                failures.append(f"{inbox.communication_id}: {attempt.error}")
        if failures:
            raise InboxAdmissionError(failures)
        return tuple(attempts)

    def _consume_one(
        self,
        inbox: CommunicationInboxRecord,
        binder: InboxAdmissionBinder,
    ) -> InboxAdmissionAttempt:
        """消费单条 inbox；claim 冲突按并发契约分类，binder 失败记录。"""
        communication_id = inbox.communication_id
        try:
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
            binding = binder(InboxAdmissionTarget(inbox=inbox))
        except Exception as error:  # noqa: BLE001 —— 记录后显式暴露
            return self._record_failure(
                communication_id, f"{type(error).__name__}: {error}"
            )
        if not isinstance(binding, InboxExecutionBinding):
            return self._record_failure(
                communication_id,
                f"binder 返回类型非法: {type(binding).__name__}",
            )
        try:
            bound = self._store.mark_communication_inbox_execution_bound(
                communication_id,
                job_id=binding.job_id,
                turn_id=binding.turn_id,
                claim_owner=self._claim_owner,
                claim_generation=self._claim_generation,
            )
        except (ValueError, RuntimeError, KeyError) as error:
            return self._record_failure(communication_id, f"{error}")
        return InboxAdmissionAttempt(
            communication_id=communication_id,
            outcome="bound",
            bound_inbox=bound,
        )

    def _record_failure(
        self,
        communication_id: str,
        message: str,
    ) -> InboxAdmissionAttempt:
        """记录 last_error 并保持 target_accepted 可恢复；返回失败 attempt。"""
        try:
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
