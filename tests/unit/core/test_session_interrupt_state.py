from __future__ import annotations

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
