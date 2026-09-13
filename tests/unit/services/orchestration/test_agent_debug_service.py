from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import JobEventBus
from app.services.mapping.trace_event_mapper import TraceEventMapper
from app.services.orchestration.agent_debug_service import AgentDebugService


async def _wait_for_event(
    bus: JobEventBus,
    job_id: str,
    event_type: str,
    *,
    minimum_count: int = 1,
) -> None:
    for _ in range(50):
        events = await bus.list_events(job_id)
        if sum(event.type == event_type for event in events) >= minimum_count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"未收到事件: job_id={job_id} event_type={event_type}")


@pytest.mark.asyncio
async def test_breakpoint_pauses_before_tool_and_continue_releases_gate() -> None:
    bus = JobEventBus()
    service = AgentDebugService(job_event_bus=bus)
    job_id = "job_debug_before"
    session_id = "session_debug_before"

    state = await service.apply_action(
        job_id=job_id,
        session_id=session_id,
        action="debug_set_breakpoint",
        params={"kind": "tool_before", "tool_name": "run_tests"},
    )
    assert state.breakpoints[0].kind == "tool_before"

    stop_task = asyncio.create_task(
        service.maybe_stop(
            job_id=job_id,
            session_id=session_id,
            point="tool_before",
            tool_name="run_tests",
            args={"command": "pytest"},
        )
    )
    await _wait_for_event(bus, job_id, "debug_stop")

    stopped = await service.get_state(job_id, session_id)
    assert stopped.paused is True
    assert stopped.active_stop is not None
    assert stopped.active_stop.tool_name == "run_tests"
    assert stopped.active_stop.args == {"command": "pytest"}
    stop_event = next(
        event for event in await bus.list_events(job_id) if event.type == "debug_stop"
    )
    mapped_stop = TraceEventMapper().map_one(
        stop_event.model_dump(mode="json"),
        session_id=session_id,
    )
    assert mapped_stop is not None
    assert mapped_stop.phase == "debug"
    assert mapped_stop.tool_name == "run_tests"

    await service.apply_action(
        job_id=job_id,
        session_id=session_id,
        action="debug_continue",
    )
    await asyncio.wait_for(stop_task, timeout=1)
    assert (await service.get_state(job_id, session_id)).paused is False

    events = await bus.list_events(job_id)
    assert [event.type for event in events].count("debug_action") >= 2


@pytest.mark.asyncio
async def test_step_tool_stops_after_next_tool_without_configured_breakpoint() -> None:
    bus = JobEventBus()
    service = AgentDebugService(job_event_bus=bus)
    job_id = "job_debug_step"
    session_id = "session_debug_step"

    state = await service.apply_action(
        job_id=job_id,
        session_id=session_id,
        action="debug_set_breakpoint",
        params={"kind": "tool_before"},
    )
    breakpoint_id = state.breakpoints[0].breakpoint_id
    first_stop = asyncio.create_task(
        service.maybe_stop(
            job_id=job_id,
            session_id=session_id,
            point="tool_before",
            tool_name="read_file",
            args={"file_path": "README.md"},
        )
    )
    await _wait_for_event(bus, job_id, "debug_stop")
    await service.apply_action(
        job_id=job_id,
        session_id=session_id,
        action="debug_step_tool",
    )
    await asyncio.wait_for(first_stop, timeout=1)

    second_stop = asyncio.create_task(
        service.maybe_stop(
            job_id=job_id,
            session_id=session_id,
            point="tool_after",
            tool_name="read_file",
            args={"file_path": "README.md"},
            result="content",
        )
    )
    await _wait_for_event(bus, job_id, "debug_stop", minimum_count=2)
    state = await service.get_state(job_id, session_id)
    assert state.active_stop is not None
    assert state.active_stop.reason == "single_step"
    assert state.active_stop.breakpoint_id is None
    assert breakpoint_id in {item.breakpoint_id for item in state.breakpoints}

    await service.apply_action(
        job_id=job_id,
        session_id=session_id,
        action="debug_continue",
    )
    await asyncio.wait_for(second_stop, timeout=1)
