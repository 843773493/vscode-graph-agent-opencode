"""初始 execution intent 消费 worker（OpenSpec 8.5-B，R23）。

对应 ``add-itemized-rollout-context`` 任务 8.5-B 与
``rounds/R23-task-brief.md``：

- worker 只消费 :class:`SessionControlStore` 的持久状态索引
  （``list_pending_initial_execution_intents``），不扫磁盘、不读
  ``thread.json``、不感知当前 Agent 配置或 task seed。
- binder 是可注入协议；binder 输入携带冻结 intent 与软件生成的稳定
  ``execution_binding_id`` / ``job_id``。R25/R26 提供真实 thread
  binder 前，本模块不提供默认 binder，也不注册任何会启动父 Session
  Job 的实现（红线：R23 不启动真实 thread Job）。
- binder 成功返回的 identity 必须与冻结值完全一致才 ``mark bound``；
  binder 异常或 identity 漂移经 ``record_initial_execution_failure``
  记录后在本轮结束时显式抛出，绝不静默吞掉。
- 并发 worker 对同一 intent 只能产生一次有效 binding：claim 由 store
  的单事务闸门保证（不同 claim 冲突 fail closed）。
- 生产装配只建立 worker 生命周期（显式 start/stop 入口）；缺 binder
  时启动明确报告 unavailable，不把任何 intent 标成 bound。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.session_control_store import (
    SessionControlStore,
    ThreadExecutionIntent,
)

__all__ = [
    "InitialExecutionBindOutcome",
    "InitialExecutionBinder",
    "InitialExecutionBindingAttempt",
    "InitialExecutionBindingError",
    "InitialExecutionBindingTarget",
    "InitialExecutionBindingWorker",
]


@dataclass(frozen=True, slots=True)
class InitialExecutionBindingTarget:
    """binder 输入：冻结 intent + 稳定 binding/job identity。

    ``intent`` 是 store 状态索引的冻结投影（含身份、initial state、
    preimage hash 与稳定 identity）；binder 不得读取磁盘或进程内闭包
    补齐身份——R25/R26 的真实 thread binder 以该输入重新通过生命周期
    准入后建立 thread-qualified execution。
    """

    intent: ThreadExecutionIntent
    execution_binding_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class InitialExecutionBindOutcome:
    """binder 成功返回：binder 实际绑定的 identity。

    worker 将其与 ``InitialExecutionBindingTarget`` 冻结值逐字段比对，
    完全一致才允许 ``mark_initial_execution_bound``。
    """

    execution_binding_id: str
    job_id: str


class InitialExecutionBinder(Protocol):
    """可注入 binder 协议：绑定一个冻结 intent，返回实际绑定 identity。

    失败直接抛异常；worker 负责记录 ``last_error`` 并显式暴露。
    """

    def __call__(
        self, target: InitialExecutionBindingTarget
    ) -> InitialExecutionBindOutcome: ...


@dataclass(frozen=True, slots=True)
class InitialExecutionBindingAttempt:
    """单条 intent 的单次消费结果。

    ``outcome`` 闭集：``bound``（本轮完成绑定）、
    ``skipped_claim_held``（claim 被其他 worker 持有——并发契约的
    正常输家）、``skipped_already_bound``（并发契约的正常输家：
    列出后已被其他 worker 绑定）。``error`` 仅在 binder 失败路径
    非空（该路径本轮结束时统一抛 :class:`InitialExecutionBindingError``）。
    """

    admission_idempotency_key: str
    outcome: str
    bound_intent: ThreadExecutionIntent | None = None
    error: str | None = None


class InitialExecutionBindingError(RuntimeError):
    """单轮消费存在显式失败（binder 异常 / identity 漂移 / store 拒绝）。

    失败明细已逐条写入 intent ``last_error``；本异常是"显式暴露"
    通道，绝不静默吞掉。
    """

    def __init__(self, failures: Sequence[str]) -> None:
        super().__init__(
            "initial execution binding 单轮消费存在显式失败（已记录 "
            f"last_error，不静默吞掉）: {'; '.join(failures)}"
        )


class InitialExecutionBindingWorker:
    """单一初始 execution intent 消费 worker（显式生命周期）。

    - ``start()``：显式启动入口；缺 binder 明确报告 unavailable，
      不消费任何 intent、不把任何 intent 标成 bound。
    - ``stop()``：显式关闭入口；幂等 no-op。
    - ``bind_pending_once()``：单轮消费全部 ``pending`` intent；
      必须先 ``start()``。崩溃恢复以同一 ``(claim_owner,
      claim_generation)``` 幂等重入，或同 owner 更高 generation 接管。
    """

    def __init__(
        self,
        *,
        store: SessionControlStore,
        claim_owner: str,
        claim_generation: int = 1,
        binder: InitialExecutionBinder | None = None,
    ) -> None:
        if not isinstance(store, SessionControlStore):
            raise TypeError(
                f"store 必须是 SessionControlStore: {store!r}"
            )
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
        """是否已注入 binder（缺省 None：R25/R26 前生产装配形态）。"""
        return self._binder is not None

    def start(self) -> None:
        """显式启动；缺 binder 明确报告 unavailable，不产生任何状态变更。"""
        if self._binder is None:
            raise RuntimeError(
                "InitialExecutionBindingWorker 缺少 binder，启动 unavailable"
                "（R25/R26 提供真实 thread binder 前不得注册会启动父 "
                "Session Job 的默认 binder）；未把任何 intent 标成 bound"
            )
        if self._started:
            raise RuntimeError("InitialExecutionBindingWorker 已启动")
        self._started = True

    def stop(self) -> None:
        """显式关闭；重复关闭是幂等 no-op。"""
        self._started = False

    def bind_pending_once(self) -> tuple[InitialExecutionBindingAttempt, ...]:
        """单轮消费全部 pending intent；失败在本轮结束后显式抛出。

        逐条流程：claim（冲突按并发契约分类跳过）→ binder → identity
        复验 → ``mark_initial_execution_bound``。binder 异常 /
        identity 漂移先写 ``last_error``（intent 保持 ``pending``
        可恢复），全部 intent 处理完后抛
        :class:`InitialExecutionBindingError`` 汇总显式暴露。
        """
        if not self._started:
            raise RuntimeError(
                "InitialExecutionBindingWorker 未启动（必须先 start()）"
            )
        if self._binder is None:
            raise RuntimeError(
                "InitialExecutionBindingWorker 缺少 binder，unavailable："
                "不消费任何 intent"
            )
        binder = self._binder
        attempts: list[InitialExecutionBindingAttempt] = []
        failures: list[str] = []
        for intent in self._store.list_pending_initial_execution_intents():
            attempt = self._consume_one(intent, binder)
            attempts.append(attempt)
            if attempt.error is not None:
                failures.append(
                    f"{intent.admission_idempotency_key}: {attempt.error}"
                )
        if failures:
            raise InitialExecutionBindingError(failures)
        return tuple(attempts)

    def _consume_one(
        self,
        intent: ThreadExecutionIntent,
        binder: InitialExecutionBinder,
    ) -> InitialExecutionBindingAttempt:
        """消费单条 intent；claim 冲突按并发契约分类，binder 失败记录。"""
        key = intent.admission_idempotency_key
        claim_kwargs = {
            "claim_owner": self._claim_owner,
            "claim_generation": self._claim_generation,
        }
        try:
            self._store.claim_initial_execution_intent(key, **claim_kwargs)
        except RuntimeError:
            # claim 被拒：区分并发契约的正常输家与真实 store 错误。
            current = self._store.get_initial_execution_intent(key)
            if current.state == "bound":
                return InitialExecutionBindingAttempt(
                    admission_idempotency_key=key,
                    outcome="skipped_already_bound",
                )
            if (
                current.state == "pending"
                and current.claim_owner is not None
                and current.claim_owner != self._claim_owner
            ):
                return InitialExecutionBindingAttempt(
                    admission_idempotency_key=key,
                    outcome="skipped_claim_held",
                )
            raise
        target = InitialExecutionBindingTarget(
            intent=intent,
            execution_binding_id=intent.execution_binding_id,
            job_id=intent.job_id,
        )
        try:
            outcome = binder(target)
        except Exception as error:  # noqa: BLE001 —— 记录后显式暴露
            return self._record_failure(
                key, f"{type(error).__name__}: {error}", claim_kwargs
            )
        if not isinstance(outcome, InitialExecutionBindOutcome):
            return self._record_failure(
                key,
                f"binder 返回类型非法: {type(outcome).__name__}",
                claim_kwargs,
            )
        if (
            outcome.execution_binding_id != intent.execution_binding_id
            or outcome.job_id != intent.job_id
        ):
            return self._record_failure(
                key,
                "binder 返回 identity 与冻结值漂移: "
                f"expected=({intent.execution_binding_id!r}, "
                f"{intent.job_id!r}), "
                f"actual=({outcome.execution_binding_id!r}, "
                f"{outcome.job_id!r})",
                claim_kwargs,
            )
        bound = self._store.mark_initial_execution_bound(
            key,
            execution_binding_id=intent.execution_binding_id,
            job_id=intent.job_id,
            **claim_kwargs,
        )
        return InitialExecutionBindingAttempt(
            admission_idempotency_key=key,
            outcome="bound",
            bound_intent=bound,
        )

    def _record_failure(
        self,
        admission_idempotency_key: str,
        message: str,
        claim_kwargs: dict[str, object],
    ) -> InitialExecutionBindingAttempt:
        """写 ``last_error``（intent 保持 pending 可恢复）并返回失败
        attempt；调用方在本轮结束时统一显式抛出。"""
        self._store.record_initial_execution_failure(
            admission_idempotency_key,
            last_error=message,
            **claim_kwargs,  # type: ignore[arg-type]
        )
        return InitialExecutionBindingAttempt(
            admission_idempotency_key=admission_idempotency_key,
            outcome="error",
            error=message,
        )
