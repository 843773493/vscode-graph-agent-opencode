"""wait_for_session 合同测试：selector、timeout 边界与 deadline 裁决。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.errors import CommunicationContractError
from app.services.business.communication.wait import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    DurableDeadline,
    WaitObservation,
    WaitSelector,
    aggregate_wait_status,
    freeze_wait_deadline,
    remaining_wait_budget_seconds,
    resolve_wait_outcome,
)


@dataclass
class FakeWaitClock:
    """合同测试用的 fake 时钟：origin + 单调纳秒 + UTC 映射。"""

    origin: str = "boot-1"
    mono_ns: int = 1_000_000_000
    utc: datetime = field(default_factory=lambda: datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC))

    @property
    def origin_id(self) -> str:
        return self.origin

    def monotonic_ns(self) -> int:
        return self.mono_ns

    def utc_now(self) -> datetime:
        return self.utc

    def advance(self, *, seconds: float = 0.0) -> None:
        self.mono_ns += int(seconds * 1_000_000_000)
        self.utc += timedelta(seconds=seconds)


def make_target() -> GlobalThreadAddress:
    return GlobalThreadAddress(
        gateway_id="gw_local",
        workspace_id="ws_main",
        session_id=f"ses_{uuid.uuid4().hex}",
        thread_id=f"thr_{uuid.uuid4().hex}",
    )


def make_observation(state: str, revision: str = "rev-1") -> WaitObservation:
    return WaitObservation(
        selector_kind="job", selector_id="job_1", state=state, revision=revision  # type: ignore[arg-type]
    )


def test_selector_rejects_blank_id() -> None:
    with pytest.raises(CommunicationContractError, match="wait-selector-invalid"):
        WaitSelector(kind="job", selector_id="  ")


def test_timeout_boundaries() -> None:
    clock = FakeWaitClock()
    with pytest.raises(CommunicationContractError, match="wait-timeout-out-of-range"):
        freeze_wait_deadline(clock=clock, timeout_seconds=0)
    with pytest.raises(CommunicationContractError, match="wait-timeout-out-of-range"):
        freeze_wait_deadline(clock=clock, timeout_seconds=301)
    for timeout in (1, DEFAULT_WAIT_TIMEOUT_SECONDS, 300):
        deadline = freeze_wait_deadline(clock=clock, timeout_seconds=timeout)
        assert deadline.timeout_seconds == timeout


def test_freeze_deadline_records_utc_and_monotonic_origin() -> None:
    clock = FakeWaitClock()
    deadline = freeze_wait_deadline(clock=clock, timeout_seconds=60)
    assert deadline.monotonic_origin_id == "boot-1"
    assert deadline.monotonic_deadline_ns == clock.mono_ns + 60 * 1_000_000_000
    assert datetime.fromisoformat(deadline.deadline_at_utc) - datetime.fromisoformat(
        deadline.admitted_at_utc
    ) == timedelta(seconds=60)


def test_same_origin_restart_keeps_original_monotonic_budget() -> None:
    clock = FakeWaitClock()
    deadline = freeze_wait_deadline(clock=clock, timeout_seconds=60)
    clock.advance(seconds=30)
    assert remaining_wait_budget_seconds(deadline, clock) == pytest.approx(30.0)
    clock.advance(seconds=31)
    assert remaining_wait_budget_seconds(deadline, clock) == pytest.approx(-1.0)


def test_origin_change_budget_capped_by_original_deadline() -> None:
    clock = FakeWaitClock()
    deadline = freeze_wait_deadline(clock=clock, timeout_seconds=300)
    clock.advance(seconds=290)
    # origin 变化后只能按 UTC 计算剩余，且不超过原 timeout。
    clock.origin = "boot-2"
    remaining = remaining_wait_budget_seconds(deadline, clock)
    assert remaining is not None
    assert remaining <= 300
    clock.advance(seconds=20)
    assert remaining_wait_budget_seconds(deadline, clock) is None


def test_origin_change_clock_rollback_returns_none() -> None:
    clock = FakeWaitClock()
    deadline = freeze_wait_deadline(clock=clock, timeout_seconds=60)
    clock.origin = "boot-2"
    clock.utc = clock.utc - timedelta(seconds=10)
    assert remaining_wait_budget_seconds(deadline, clock) is None


def test_durable_deadline_rejects_naive_utc_and_negative_monotonic() -> None:
    with pytest.raises(CommunicationContractError, match="deadline-record-invalid"):
        DurableDeadline(
            timeout_seconds=60,
            admitted_at_utc="2026-09-19T12:00:00",
            deadline_at_utc="2026-09-19T12:01:00",
            monotonic_origin_id="boot-1",
            monotonic_deadline_ns=1,
        )


def test_aggregate_priority_fixed_order() -> None:
    assert aggregate_wait_status([]) == "idle"
    assert aggregate_wait_status(["completed"]) == "completed"
    assert aggregate_wait_status(["pending", "running"]) == "running"
    assert aggregate_wait_status(["completed", "cancelled"]) == "cancelled"
    assert (
        aggregate_wait_status(["running", "pending", "failed", "completed"]) == "failed"
    )


def test_empty_observation_is_idle_not_timed_out() -> None:
    result = resolve_wait_outcome(
        target=make_target(),
        observed=(),
        baseline_revision="rev-0",
        latest_revision="rev-0",
        until="terminal",
        deadline_expired=True,
        state_changed=False,
    )
    assert result.status == "idle"
    assert result.observed == ()


def test_terminal_wait_returns_aggregate_when_all_terminal() -> None:
    target = make_target()
    observed = (make_observation("completed"), make_observation("completed"))
    result = resolve_wait_outcome(
        target=target,
        observed=observed,
        baseline_revision="rev-0",
        latest_revision="rev-1",
        until="terminal",
        deadline_expired=False,
        state_changed=False,
    )
    assert result.status == "completed"
    assert result.target == target


def test_deadline_race_prefers_committed_revision_over_timed_out() -> None:
    # deadline 到期与终态并发：以 target owner 已提交 revision 裁决，返回状态。
    observed = (make_observation("completed", revision="rev-2"),)
    result = resolve_wait_outcome(
        target=make_target(),
        observed=observed,
        baseline_revision="rev-0",
        latest_revision="rev-2",
        until="terminal",
        deadline_expired=True,
        state_changed=False,
    )
    assert result.status == "completed"


def test_deadline_expiry_without_terminal_returns_timed_out_with_real_states() -> None:
    observed = (make_observation("running"), make_observation("pending"))
    result = resolve_wait_outcome(
        target=make_target(),
        observed=observed,
        baseline_revision="rev-0",
        latest_revision="rev-0",
        until="terminal",
        deadline_expired=True,
        state_changed=False,
    )
    assert result.status == "timed_out"
    assert tuple(item.state for item in result.observed) == ("running", "pending")


def test_state_change_wait_resolves_on_revision_change() -> None:
    result = resolve_wait_outcome(
        target=make_target(),
        observed=(make_observation("running", revision="rev-3"),),
        baseline_revision="rev-0",
        latest_revision="rev-3",
        until="state_change",
        deadline_expired=False,
        state_changed=True,
    )
    assert result.status == "running"


def test_state_change_wait_times_out_without_revision_change() -> None:
    result = resolve_wait_outcome(
        target=make_target(),
        observed=(make_observation("pending"),),
        baseline_revision="rev-0",
        latest_revision="rev-0",
        until="state_change",
        deadline_expired=True,
        state_changed=False,
    )
    assert result.status == "timed_out"
