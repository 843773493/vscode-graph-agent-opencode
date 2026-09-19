"""跨 Session wait_for_session 的 typed 合同。

覆盖 selector（至多一个）、timeout 边界（1-300 秒，默认 60）、
可恢复 DurableDeadline（UTC/monotonic origin 分离）与状态聚合/
deadline 竞态裁决。等待由可注入单调 Clock/DeadlineTimer 和目标状态
订阅驱动，本层不轮询数据库、不订阅未来 Job、不注入目标 context。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.errors import CommunicationContractError

DEFAULT_WAIT_TIMEOUT_SECONDS = 60
MIN_WAIT_TIMEOUT_SECONDS = 1
MAX_WAIT_TIMEOUT_SECONDS = 300

WaitSelectorKind = Literal["communication", "job", "turn"]
WaitUntil = Literal["terminal", "state_change"]
WaitState = Literal["pending", "running", "completed", "failed", "cancelled"]
WaitTopStatus = Literal[
    "idle", "pending", "running", "completed", "failed", "cancelled", "timed_out"
]

_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
# 统一聚合优先级：failed > cancelled > running > pending > completed。
_AGGREGATION_PRIORITY: tuple[str, ...] = (
    "failed",
    "cancelled",
    "running",
    "pending",
    "completed",
)


@dataclass(frozen=True, slots=True)
class WaitSelector:
    """wait 的显式 selector；模型侧至多传一个（communication_id|job_id|turn_id）。"""

    kind: WaitSelectorKind
    selector_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.selector_id, str) or not self.selector_id.strip():
            raise CommunicationContractError(
                "wait-selector-invalid",
                f"wait selector_id 必须是非空字符串: {self.selector_id!r}",
            )


@dataclass(frozen=True, slots=True)
class CommunicationWaitBinding:
    """communication selector 解析出的 target execution binding。"""

    target_session_id: str
    target_main_thread_id: str
    job_id: str | None
    turn_id: str | None

    def __post_init__(self) -> None:
        validate_session_id(self.target_session_id)
        validate_thread_id(self.target_main_thread_id)
        if self.job_id is not None and not self.job_id.strip():
            raise CommunicationContractError(
                "wait-binding-invalid",
                "CommunicationWaitBinding.job_id 必须是非空字符串",
            )
        if self.turn_id is not None and not self.turn_id.strip():
            raise CommunicationContractError(
                "wait-binding-invalid",
                "CommunicationWaitBinding.turn_id 必须是非空字符串",
            )


class WaitClock(Protocol):
    """可注入的等待时钟：单调 origin + 单调值 + UTC 映射。"""

    @property
    def origin_id(self) -> str:
        """当前 host boot/clock origin 的稳定标识；重启后变化。"""
        ...

    def monotonic_ns(self) -> int:
        """当前单调时钟值（纳秒）。"""
        ...

    def utc_now(self) -> datetime:
        """当前 UTC 时间。"""
        ...


@dataclass(frozen=True, slots=True)
class DurableDeadline:
    """可持久化的等待预算；monotonic 数值必须与 origin 绑定，禁止裸数值。"""

    timeout_seconds: int
    admitted_at_utc: str
    deadline_at_utc: str
    monotonic_origin_id: str
    monotonic_deadline_ns: int

    def __post_init__(self) -> None:
        _validate_timeout(self.timeout_seconds)
        if not self.monotonic_origin_id.strip():
            raise CommunicationContractError(
                "deadline-record-invalid",
                "DurableDeadline.monotonic_origin_id 不能为空",
            )
        if self.monotonic_deadline_ns < 0:
            raise CommunicationContractError(
                "deadline-record-invalid",
                f"monotonic_deadline_ns 不能为负: {self.monotonic_deadline_ns}",
            )
        admitted = _parse_utc(self.admitted_at_utc, "admitted_at_utc")
        deadline = _parse_utc(self.deadline_at_utc, "deadline_at_utc")
        if deadline < admitted:
            raise CommunicationContractError(
                "deadline-record-invalid",
                f"deadline_at_utc 早于 admitted_at_utc: {self.deadline_at_utc}",
            )


def freeze_wait_deadline(
    *,
    clock: WaitClock,
    timeout_seconds: int,
) -> DurableDeadline:
    """按当前时钟冻结一次等待预算；timeout 越界报 wait-timeout-out-of-range。"""
    _validate_timeout(timeout_seconds)
    admitted_ns = clock.monotonic_ns()
    admitted_utc = clock.utc_now()
    if admitted_utc.tzinfo is None:
        raise CommunicationContractError(
            "deadline-clock-unavailable",
            "clock.utc_now() 必须返回带时区的 UTC 时间",
    )
    deadline_utc = admitted_utc + timedelta(seconds=timeout_seconds)
    return DurableDeadline(
        timeout_seconds=timeout_seconds,
        admitted_at_utc=admitted_utc.astimezone(UTC).isoformat(),
        deadline_at_utc=deadline_utc.astimezone(UTC).isoformat(),
        monotonic_origin_id=clock.origin_id,
        monotonic_deadline_ns=admitted_ns + timeout_seconds * 1_000_000_000,
    )


def remaining_wait_budget_seconds(
    deadline: DurableDeadline,
    clock: WaitClock,
) -> float | None:
    """计算剩余等待预算（秒）。

    - origin 相同：继续原 monotonic deadline，重启不延长预算。
    - origin 变化：只能用可信 UTC 计算不超过原 deadline/timeout 的剩余；
      无法证明仍有正剩余、检测到回拨时返回 None（按 timed_out 处理）。
    """
    if clock.origin_id == deadline.monotonic_origin_id:
        remaining_ns = deadline.monotonic_deadline_ns - clock.monotonic_ns()
        return remaining_ns / 1_000_000_000
    now = clock.utc_now()
    admitted = _parse_utc(deadline.admitted_at_utc, "admitted_at_utc")
    if now < admitted:
        return None
    deadline_utc = _parse_utc(deadline.deadline_at_utc, "deadline_at_utc")
    remaining = (deadline_utc - now).total_seconds()
    if remaining <= 0:
        return None
    return min(remaining, float(deadline.timeout_seconds))


@dataclass(frozen=True, slots=True)
class WaitObservation:
    """一个被观察对象的当前状态；selector_kind/selector_id 标明来源。"""

    selector_kind: WaitSelectorKind
    selector_id: str
    state: WaitState
    revision: str

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise CommunicationContractError(
                "wait-selector-invalid",
                "WaitObservation.revision 必须是非空字符串",
            )


@dataclass(frozen=True, slots=True)
class WaitForSessionResult:
    """wait_for_session 的返回合同；observed 保留真实状态，不猜测未返回对象。"""

    status: WaitTopStatus
    target: GlobalThreadAddress
    observed: tuple[WaitObservation, ...]
    baseline_revision: str
    latest_revision: str

    def __post_init__(self) -> None:
        for field_name in ("baseline_revision", "latest_revision"):
            if not getattr(self, field_name).strip():
                raise CommunicationContractError(
                    "wait-selector-invalid",
                    f"WaitForSessionResult.{field_name} 必须是非空字符串",
                )


def aggregate_wait_status(states: tuple[WaitState, ...] | list[WaitState]) -> WaitTopStatus:
    """按固定优先级聚合观察状态；空集合返回 idle。"""
    for state in _AGGREGATION_PRIORITY:
        if state in states:
            return state  # type: ignore[return-value]
    if len(states) == 0:
        return "idle"
    raise ValueError(f"未知等待状态: {states!r}")


def resolve_wait_status(
    *,
    observed: tuple[WaitObservation, ...] | list[WaitObservation],
    until: WaitUntil,
    deadline_expired: bool,
    state_changed: bool,
) -> WaitTopStatus:
    """纯状态裁决（不含 target/baseline 的轻量形态，供工具层使用）。

    deadline 与 terminal/state-change 并发时，以 target owner 已提交的
    revision 裁决：条件已满足则返回该状态，否则 timed_out。
    """
    if until not in ("terminal", "state_change"):
        raise CommunicationContractError(
            "wait-until-invalid",
            f"until 必须是 terminal|state_change: {until!r}",
        )
    if not observed:
        return "idle"
    states = tuple(item.state for item in observed)
    if until == "terminal":
        condition_met = all(state in _TERMINAL_STATES for state in states)
    else:
        condition_met = state_changed
    if deadline_expired and not condition_met:
        return "timed_out"
    return aggregate_wait_status(list(states))


def resolve_wait_outcome(
    *,
    target: GlobalThreadAddress,
    observed: tuple[WaitObservation, ...] | list[WaitObservation],
    baseline_revision: str,
    latest_revision: str,
    until: WaitUntil,
    deadline_expired: bool,
    state_changed: bool,
) -> WaitForSessionResult:
    """裁决一次 wait 的结果。

    deadline 与 terminal/state-change 并发时，以 target owner 已提交的
    revision 裁决：条件已满足则返回该状态，否则 timed_out；observed 始终
    保留真实状态。
    """
    if until not in ("terminal", "state_change"):
        raise CommunicationContractError(
            "wait-until-invalid",
            f"until 必须是 terminal|state_change: {until!r}",
        )
    if not observed:
        return WaitForSessionResult(
            status="idle",
            target=target,
            observed=(),
            baseline_revision=baseline_revision,
            latest_revision=latest_revision,
        )
    states = tuple(item.state for item in observed)
    if until == "terminal":
        condition_met = all(state in _TERMINAL_STATES for state in states)
    else:
        condition_met = state_changed
    if deadline_expired and not condition_met:
        status: WaitTopStatus = "timed_out"
    else:
        status = aggregate_wait_status(list(states))
    return WaitForSessionResult(
        status=status,
        target=target,
        observed=tuple(observed),
        baseline_revision=baseline_revision,
        latest_revision=latest_revision,
    )


def _validate_timeout(timeout_seconds: int) -> None:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise CommunicationContractError(
            "wait-timeout-out-of-range",
            f"timeout_seconds 必须是整数: {timeout_seconds!r}",
        )
    if not MIN_WAIT_TIMEOUT_SECONDS <= timeout_seconds <= MAX_WAIT_TIMEOUT_SECONDS:
        raise CommunicationContractError(
            "wait-timeout-out-of-range",
            f"timeout_seconds 必须在 {MIN_WAIT_TIMEOUT_SECONDS}-"
            f"{MAX_WAIT_TIMEOUT_SECONDS} 秒之间: {timeout_seconds}",
        )


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CommunicationContractError(
            "deadline-record-invalid",
            f"DurableDeadline.{field_name} 不是合法 ISO 时间: {value!r}",
        ) from error
    if parsed.tzinfo is None:
        raise CommunicationContractError(
            "deadline-record-invalid",
            f"DurableDeadline.{field_name} 必须带 UTC 时区: {value!r}",
        )
    return parsed.astimezone(UTC)
