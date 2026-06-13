from __future__ import annotations

import contextvars

_current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_job_id", default=None)
_current_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_agent_id", default=None)


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
