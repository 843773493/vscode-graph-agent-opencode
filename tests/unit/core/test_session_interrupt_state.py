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


def test_no_op_state_reset_does_not_accumulate_session_keys() -> None:
    """全默认态与「无该 session」语义等价，不得为无状态会话留下键。

    ``SessionInterruptState`` 是进程级长驻表：若全默认写入也落键，长驻进程
    内存会随历史会话数无界增长（runner 在进入 try 之前的重置、以及
    ``current_text=""`` 之类空写都属于此类）。上界必须与历史会话数无关。
    """
    SessionInterruptState._states.clear()
    try:
        for index in range(5000):
            SessionInterruptState.set(
                "ses_noop_" + str(index),
                phase=None,
                tool_name=None,
                clear_active_tools=True,
            )
        # 上界恒定：全默认态不落键，不随会话数增长。
        assert len(SessionInterruptState._states) == 0
        # 无状态会话 get 仍返回全默认态（语义不变）。
        state = SessionInterruptState.get("ses_noop_0")
        assert state.phase is None
        assert state.tool_name is None
        assert state.active_tool_names == ()
    finally:
        SessionInterruptState._states.clear()


def test_real_state_is_recalled_and_reclaimed() -> None:
    """有实质状态的会话仍恒定命中；clear 后回收键，上界不随历史会话增长。"""
    SessionInterruptState._states.clear()
    try:
        SessionInterruptState.set("ses_real", phase="tool", tool_name="read_file")
        assert len(SessionInterruptState._states) == 1
        # 同 session 恒定命中。
        assert SessionInterruptState.get("ses_real").phase == "tool"
        assert SessionInterruptState.get("ses_real").tool_name == "read_file"
        # 回收：clear 释放键。
        SessionInterruptState.clear("ses_real")
        assert len(SessionInterruptState._states) == 0
    finally:
        SessionInterruptState._states.clear()
