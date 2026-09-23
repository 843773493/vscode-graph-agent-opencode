from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.tools.session_wait import create_wait_for_session_tool

_SESSION_ID = "ses_5a2c9d1f0e3b4a7c8d9e0f1a2b3c4d5e"


class _FakeJob:
    def __init__(self, job_id: str, status: str) -> None:
        self.job_id = job_id
        self.created_at = datetime.now(UTC)
        self.status = status


class _ProgrammableJobService:
    """可编程 Job 状态序列；每次观察推进一格，末态保持。"""

    def __init__(self, *, job_id: str, states: list[str]) -> None:
        self._job_id = job_id
        self._states = states
        self._cursor = 0

    async def list(self, session_id=None):
        state = self._states[min(self._cursor, len(self._states) - 1)]
        self._cursor += 1
        return [_FakeJob(self._job_id, state)]


class _NeverBoundLookup:
    async def resolve(self, *, target_session_id, communication_id):
        return None


async def test_state_change_wait_returns_on_real_wait_state_transition() -> None:
    """queued(pending) → running 是 wait 状态闭集中的真实变化，必须立即返回。"""
    service = _ProgrammableJobService(
        job_id="job_sc_1",
        states=["queued", "running", "running", "running"],
    )
    tool = create_wait_for_session_tool(
        _SESSION_ID,
        job_service=service,
        binding_lookup=_NeverBoundLookup(),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": _SESSION_ID,
            "job_id": "job_sc_1",
            "until": "state_change",
            "timeout_seconds": 5,
        }
    )

    assert result["status"] == "running"
    assert result["baseline_revision"] == "job:job_sc_1:pending"
    assert result["latest_revision"] == "job:job_sc_1:running"
    assert result["observed"][0]["state"] == "running"


async def test_state_change_wait_reports_stable_revision_while_unchanged() -> None:
    """状态未变化时 revision 稳定，超时返回 timed_out 且保留真实状态。"""
    service = _ProgrammableJobService(job_id="job_sc_2", states=["running"])
    tool = create_wait_for_session_tool(
        _SESSION_ID,
        job_service=service,
        binding_lookup=_NeverBoundLookup(),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": _SESSION_ID,
            "job_id": "job_sc_2",
            "until": "state_change",
            "timeout_seconds": 1,
        }
    )

    assert result["status"] == "timed_out"
    assert result["baseline_revision"] == "job:job_sc_2:running"
    assert result["latest_revision"] == "job:job_sc_2:running"
    assert result["observed"][0]["state"] == "running"


@pytest.mark.parametrize("until", ["terminal", "state_change"])
async def test_communication_unbound_selector_reports_communication_kind(until: str) -> None:
    """未 execution-bound 的 communication 不得被伪报为 job 观察。"""

    class _UnboundLookup:
        async def resolve(self, *, target_session_id, communication_id):
            from app.agents.tools.session_wait import CommunicationWaitBinding

            return CommunicationWaitBinding(
                target_session_id=_SESSION_ID,
                target_main_thread_id="thr_4f9d3a1e8b2c4d5f8a7b6c5d4e3f2a1b",
                job_id=None,
                turn_id=None,
            )

    tool = create_wait_for_session_tool(
        _SESSION_ID,
        job_service=_ProgrammableJobService(job_id="job_ignored", states=["running"]),
        binding_lookup=_UnboundLookup(),
    )

    result = await tool.ainvoke(
        {
            "target_session_id": _SESSION_ID,
            "communication_id": "comm_pending",
            "until": until,
            "timeout_seconds": 1,
        }
    )

    assert result["status"] == "timed_out"
    assert result["observed"][0]["selector_kind"] == "communication"
    assert result["observed"][0]["state"] == "pending"
