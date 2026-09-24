"""wait_for_session 工具：有界、可恢复 selector 的跨 Session 等待。

OpenSpec add-context-injection-lifecycle 4.7/E03：selector 至多一个（communication_id|job_id|turn_id），
timeout 1-300 秒默认 60，由可注入单调 Clock 驱动；无 selector 时冻结
准入快照中已经存在的 identity 集合，不订阅未来 Job。deadline 与
terminal/state-change 并发时以 target 已提交状态裁决（typed 合同见
app/services/business/communication/wait.py）。
当前观察实现按有界间隔轮询 JobService 快照（与旧 monitor 的订阅管理
等价能力），事件订阅驱动属后续切片；communication/turn selector 的
execution binding 经 typed lookup port 解析，生产实现在装配层接线。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from langchain_core.tools import BaseTool, tool
from pydantic import Field

from app.abstractions.job_service import JobServiceProtocol
from app.core.session_catalog_store import validate_session_id
from app.services.business.communication.wait import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    CommunicationWaitBinding,
    WaitObservation,
    WaitSelector,
    WaitSelectorKind,
    WaitState,
    freeze_wait_deadline,
    remaining_wait_budget_seconds,
    resolve_wait_status,
)

__all__ = [
    "CommunicationWaitBinding",
    "CommunicationWaitBindingLookupPort",
    "JobServiceSessionWaitObservationPort",
    "SessionWaitObservationPort",
    "create_wait_for_session_tool",
]

_POLL_INTERVAL_SECONDS = 1.0

# JobStatus → wait 状态闭集映射（pending/running 为非终态，其余为终态）。
_JOB_STATUS_TO_WAIT_STATE: dict[str, WaitState] = {
    "accepted": "pending",
    "queued": "pending",
    "running": "running",
    "streaming": "running",
    "waiting_input": "running",
    "paused": "running",
    "interrupt_pending": "running",
    "cancelling": "running",
    "completed": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "failed",
}


class CommunicationWaitBindingLookupPort(Protocol):
    """(target_session_id, communication_id) → execution binding。

    生产实现冷读 target session-control inbox（不加载目标 runtime）；
    未知 communication 返回 None（工具层转 selector_not_found）。
    """

    async def resolve(
        self, *, target_session_id: str, communication_id: str
    ) -> CommunicationWaitBinding | None: ...


class SessionWaitObservationPort(Protocol):
    """target session 的 Job 状态观察 port；返回 job_id → wait 状态。"""

    async def observe_jobs(
        self, *, target_session_id: str, job_ids: frozenset[str] | None
    ) -> dict[str, WaitState]: ...


class JobServiceSessionWaitObservationPort(SessionWaitObservationPort):
    """生产观察实现：JobService 快照映射到 wait 状态闭集。"""

    def __init__(self, job_service: JobServiceProtocol) -> None:
        self._job_service = job_service

    async def observe_jobs(
        self, *, target_session_id: str, job_ids: frozenset[str] | None
    ) -> dict[str, WaitState]:
        jobs = await self._job_service.list(session_id=target_session_id)
        states: dict[str, WaitState] = {}
        for job in jobs:
            if job_ids is not None and job.job_id not in job_ids:
                continue
            # JobStatus 继承 str，成员与字符串键的 hash/eq 一致，可直接查表；
            # 不能用 str(job.status)——str-Enum 的 __str__ 会得到
            # "JobStatus.running" 这类限定名，使任何真实状态都 miss。
            state = _JOB_STATUS_TO_WAIT_STATE.get(job.status)
            if state is None:
                raise RuntimeError(
                    f"未知 JobStatus，fail closed: {job.status!r}"
                )
            states[job.job_id] = state
        return states


class _SystemWaitClock:
    """生产等待时钟：host boot origin + 单调纳秒 + UTC。"""

    @property
    def origin_id(self) -> str:
        return f"boot-{time.time_ns() // 1_000_000_000_000}"

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)


def _make_observation(
    *, selector_kind: WaitSelectorKind, selector_id: str, state: WaitState
) -> WaitObservation:
    return WaitObservation(
        selector_kind=selector_kind,
        selector_id=selector_id,
        state=state,
        revision=f"{selector_kind}:{selector_id}:{state}",
    )


def _observations_revision(observations: list[WaitObservation]) -> str:
    """聚合观察快照的唯一 revision；状态变化必须体现在 revision 上。"""
    return ":".join(sorted(item.revision for item in observations)) or "none"


def create_wait_for_session_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    job_service: JobServiceProtocol,
    binding_lookup: CommunicationWaitBindingLookupPort,
) -> BaseTool:
    """创建有界 wait_for_session 工具；观察 port 可注入。"""
    observer = JobServiceSessionWaitObservationPort(job_service)

    @tool("wait_for_session")
    async def wait_for_session(
        target_session_id: str,
        communication_id: Annotated[
            str | None,
            Field(default=None, description="等待指定跨会话通信的执行完成"),
        ] = None,
        job_id: Annotated[
            str | None,
            Field(default=None, description="等待指定 Job 到终态"),
        ] = None,
        turn_id: Annotated[
            str | None,
            Field(default=None, description="等待指定 Turn 绑定的执行完成"),
        ] = None,
        until: Literal["terminal", "state_change"] = "terminal",
        timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """有界等待目标 Session 的执行状态；selector 至多传一个。

        返回状态闭集 idle|pending|running|completed|failed|cancelled|
        timed_out；timeout 1-300 秒默认 60。timeout 只把顶层状态设为
        timed_out，observed 保留真实状态。
        """
        selector_values = [
            value for value in (communication_id, job_id, turn_id) if value
        ]
        if len(selector_values) > 1:
            raise ValueError("wait selector 至多传一个（communication_id|job_id|turn_id）")
        validate_session_id(target_session_id)
        deadline = freeze_wait_deadline(
            clock=_SystemWaitClock(), timeout_seconds=timeout_seconds
        )
        selector: WaitSelector | None = None
        bound_job_ids: frozenset[str] | None = None
        communication_id_value: str | None = None
        if communication_id:
            selector = WaitSelector(kind="communication", selector_id=communication_id)
            communication_id_value = communication_id
        elif job_id:
            selector = WaitSelector(kind="job", selector_id=job_id)
            bound_job_ids = frozenset({job_id})
        elif turn_id:
            selector = WaitSelector(kind="turn", selector_id=turn_id)

        # 无 selector 时冻结准入快照的 identity 集合：首次观察到的 job id
        # 集合即为冻结范围，后续轮询只观察同一集合，绝不订阅准入之后新建
        # 的 Job（否则新 Job 会凭 revision 变化伪造 state_change 完成条件）。
        frozen_selectorless_job_ids: frozenset[str] | None = None

        async def _observe() -> tuple[dict[str, WaitState], str | None]:
            """返回当前观察快照；communication selector 返回未绑定标记。"""
            nonlocal frozen_selectorless_job_ids
            if communication_id_value is not None:
                binding = await binding_lookup.resolve(
                    target_session_id=target_session_id,
                    communication_id=communication_id_value,
                )
                if binding is None:
                    raise ValueError(
                        f"selector_not_found: communication_id={communication_id_value!r}"
                    )
                if binding.job_id is None:
                    return {}, "pending"
                return await observer.observe_jobs(
                    target_session_id=binding.target_session_id,
                    job_ids=frozenset({binding.job_id}),
                ), None
            if turn_id is not None:
                raise ValueError(
                    "selector_not_found: turn_id 观察数据源属 turn-binding "
                    f"切片: turn_id={turn_id!r}"
                )
            if selector is None:
                if frozen_selectorless_job_ids is None:
                    admission = await observer.observe_jobs(
                        target_session_id=target_session_id,
                        job_ids=None,
                    )
                    frozen_selectorless_job_ids = frozenset(admission)
                    return admission, None
                return await observer.observe_jobs(
                    target_session_id=target_session_id,
                    job_ids=frozen_selectorless_job_ids,
                ), None
            observed_jobs = await observer.observe_jobs(
                target_session_id=target_session_id,
                job_ids=bound_job_ids,
            )
            # 显式 job selector 必须能在目标 session 观察到；否则报
            # selector_not_found，绝不把未知 job_id 伪装成 idle。
            if job_id is not None and job_id not in observed_jobs:
                raise ValueError(
                    f"selector_not_found: job_id={job_id!r}"
                )
            return observed_jobs, None

        async def _snapshot() -> list[WaitObservation]:
            states, unbound = await _observe()
            if unbound == "pending":
                return [
                    _make_observation(
                        selector_kind="communication",
                        selector_id=communication_id or "",
                        state="pending",
                    )
                ]
            return [
                _make_observation(
                    selector_kind="job",
                    selector_id=job_id_,
                    state=state,
                )
                for job_id_, state in sorted(states.items())
            ]

        observed = await _snapshot()
        if selector is None and not observed:
            return {
                "status": "idle",
                "target_session_id": target_session_id,
                "observed": [],
                "baseline_revision": "none",
                "latest_revision": "none",
            }
        baseline_revision = _observations_revision(observed)

        while True:
            latest_revision = _observations_revision(observed)
            remaining = remaining_wait_budget_seconds(deadline, _SystemWaitClock())
            deadline_expired = remaining is None or remaining <= 0
            state_changed = latest_revision != baseline_revision
            status = resolve_wait_status(
                observed=observed,
                until=until,
                deadline_expired=deadline_expired,
                state_changed=state_changed,
            )
            # until=terminal 时终态聚合即为完成条件；until=state_change 时
            # 以真实 revision 变化为唯一完成条件，聚合状态可能仍是 running。
            condition_met = (
                status not in ("pending", "running")
                if until == "terminal"
                else state_changed
            )
            if condition_met or deadline_expired:
                return {
                    "status": status,
                    "target_session_id": target_session_id,
                    "observed": [
                        {
                            "selector_kind": item.selector_kind,
                            "selector_id": item.selector_id,
                            "state": item.state,
                            "revision": item.revision,
                        }
                        for item in observed
                    ],
                    "baseline_revision": baseline_revision,
                    "latest_revision": latest_revision,
                }
            budget = remaining if remaining is not None else _POLL_INTERVAL_SECONDS
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, max(budget, 0.0)))
            observed = await _snapshot()

    return wait_for_session
