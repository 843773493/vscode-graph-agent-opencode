from __future__ import annotations

import contextvars
from typing import Any

_current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_job_id", default=None)
_current_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_agent_id", default=None)
_current_last_turn_status: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_last_turn_status", default=None
)
_current_recent_tool_results: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "current_recent_tool_results", default=None
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


def get_last_turn_status() -> str | None:
    """获取当前 job 的上一 turn 状态（如 interrupted_tool / interrupted_text / completed / ok）。"""
    return _current_last_turn_status.get()


def set_last_turn_status(status: str | None) -> contextvars.Token[str | None]:
    """设置当前 job 的上一 turn 状态。"""
    return _current_last_turn_status.set(status)


def reset_last_turn_status(token: contextvars.Token[str | None]) -> None:
    """恢复之前的上一 turn 状态。"""
    _current_last_turn_status.reset(token)


def get_recent_tool_results() -> list[dict[str, Any]] | None:
    """获取当前 job 的最近工具结果快照。"""
    return _current_recent_tool_results.get()


def set_recent_tool_results(results: list[dict[str, Any]] | None) -> contextvars.Token[list[dict[str, Any]] | None]:
    """设置当前 job 的最近工具结果快照。"""
    return _current_recent_tool_results.set(results)


def reset_recent_tool_results(token: contextvars.Token[list[dict[str, Any]] | None]) -> None:
    """恢复之前的最近工具结果快照。"""
    _current_recent_tool_results.reset(token)
