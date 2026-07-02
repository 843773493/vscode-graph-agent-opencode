from __future__ import annotations

import contextvars

_current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_job_id", default=None)
_current_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_agent_id", default=None)
_current_active_tool_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_active_tool_name", default=None
)
_current_interruptible_phase: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_interruptible_phase", default=None
)


def get_current_job_id() -> str | None:
    """获取当前正在执行的 job_id，供 Agent Middleware 读取。"""
    return _current_job_id.get()


def set_current_job_id(job_id: str | None) -> contextvars.Token[str | None]:
    """设置当前 job_id，返回 token 用于恢复。"""
    return _current_job_id.set(job_id)


def reset_current_job_id(token: contextvars.Token[str | None]) -> None:
    """恢复之前的 job_id。"""
    _current_job_id.reset(token)


def get_current_agent_id() -> str | None:
    """获取当前正在执行的 agent_id，供 Agent Middleware 读取。"""
    return _current_agent_id.get()


def set_current_agent_id(agent_id: str | None) -> contextvars.Token[str | None]:
    """设置当前 agent_id，返回 token 用于恢复。"""
    return _current_agent_id.set(agent_id)


def reset_current_agent_id(token: contextvars.Token[str | None]) -> None:
    """恢复之前的 agent_id。"""
    _current_agent_id.reset(token)


def get_active_tool_name() -> str | None:
    """获取当前正在执行的工具名，供打断服务判断阶段使用。"""
    return _current_active_tool_name.get()


def set_active_tool_name(tool_name: str | None) -> contextvars.Token[str | None]:
    """设置当前正在执行的工具名。"""
    return _current_active_tool_name.set(tool_name)


def reset_active_tool_name(token: contextvars.Token[str | None]) -> None:
    """恢复之前的工具名。"""
    _current_active_tool_name.reset(token)


def get_interruptible_phase() -> str | None:
    """获取当前可中断阶段（text 或 tool）。"""
    return _current_interruptible_phase.get()


def set_interruptible_phase(phase: str | None) -> contextvars.Token[str | None]:
    """设置当前可中断阶段。"""
    return _current_interruptible_phase.set(phase)


def reset_interruptible_phase(token: contextvars.Token[str | None]) -> None:
    """恢复之前的可中断阶段。"""
    _current_interruptible_phase.reset(token)
