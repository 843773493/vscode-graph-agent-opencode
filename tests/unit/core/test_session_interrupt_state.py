from __future__ import annotations

import pytest

from app.core.session_interrupt_state import SessionInterruptState


def test_session_interrupt_state_can_clear_explicit_none() -> None:
    session_id = "ses_interrupt_state_clear"
    SessionInterruptState.clear(session_id)

    SessionInterruptState.set(
        session_id,
        phase="tool",
        tool_name="python_exec",
        current_text="处理中",
    )
    SessionInterruptState.set(
        session_id,
        phase="text",
        tool_name=None,
        current_text="",
    )

    state = SessionInterruptState.get(session_id)
    assert state.phase == "text"
    assert state.tool_name is None
    assert state.current_text == ""
    assert state.user_interrupt_reminder_injected is False

    SessionInterruptState.set(session_id, user_interrupt_reminder_injected=True)
    assert SessionInterruptState.get(session_id).user_interrupt_reminder_injected is True

    SessionInterruptState.set(session_id, phase=None)
    assert SessionInterruptState.get(session_id).phase is None

    SessionInterruptState.clear(session_id)
    assert SessionInterruptState.get(session_id).user_interrupt_reminder_injected is False


def test_parallel_tool_state_keeps_remaining_tool_active() -> None:
    session_id = "ses_parallel_tool_state"
    SessionInterruptState.clear(session_id)

    first_state = SessionInterruptState.start_tool(
        session_id,
        run_id="run_a",
        tool_name="read_file",
    )
    assert first_state.phase == "tool"
    assert first_state.tool_name == "read_file"

    parallel_state = SessionInterruptState.start_tool(
        session_id,
        run_id="run_b",
        tool_name="grep",
    )
    assert parallel_state.active_tool_names == ("read_file", "grep")
    assert parallel_state.tool_name == "read_file、grep"

    remaining_state = SessionInterruptState.end_tool(session_id, run_id="run_a")
    assert remaining_state.phase == "tool"
    assert remaining_state.tool_name == "grep"
    assert remaining_state.active_tools_by_run_id == {"run_b": "grep"}

    finished_state = SessionInterruptState.end_tool(session_id, run_id="run_b")
    assert finished_state.phase is None
    assert finished_state.tool_name is None
    assert finished_state.active_tool_names == ()
    SessionInterruptState.clear(session_id)


def test_parallel_tool_state_rejects_unknown_end_event() -> None:
    session_id = "ses_parallel_tool_unknown_end"
    SessionInterruptState.clear(session_id)

    with pytest.raises(RuntimeError, match="未登记"):
        SessionInterruptState.end_tool(session_id, run_id="missing")


def test_parallel_tool_state_rejects_legacy_phase_overwrite() -> None:
    session_id = "ses_parallel_tool_legacy_overwrite"
    SessionInterruptState.clear(session_id)
    SessionInterruptState.start_tool(
        session_id,
        run_id="run_active",
        tool_name="read_file",
    )

    with pytest.raises(RuntimeError, match="不能直接覆盖"):
        SessionInterruptState.set(session_id, phase=None, tool_name=None)

    state = SessionInterruptState.get(session_id)
    assert state.phase == "tool"
    assert state.tool_name == "read_file"
    assert state.active_tools_by_run_id == {"run_active": "read_file"}
    SessionInterruptState.clear(session_id)
