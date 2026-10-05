"""LangGraph callback 使用的显式 execution admission context。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from app.core.session_thread_context import current_session_thread_owner
from app.domain.itemized.execution_admission import ExecutionAdmission

_current_admission: ContextVar[ExecutionAdmission | None] = ContextVar(
    "current_execution_admission",
    default=None,
)


def _validate_binding(
    admission: ExecutionAdmission,
    *,
    langgraph_thread_id: str | None,
) -> ExecutionAdmission:
    if langgraph_thread_id is not None:
        if not isinstance(langgraph_thread_id, str) or not langgraph_thread_id:
            raise ValueError("LangGraph thread_id 必须是非空字符串")
        if admission.thread_id != langgraph_thread_id:
            raise ValueError(
                "execution admission 与 LangGraph thread_id 冲突: "
                f"admission=({admission.session_id},{admission.thread_id}), "
                f"langgraph_thread_id={langgraph_thread_id}"
            )
    owner = current_session_thread_owner(langgraph_thread_id=langgraph_thread_id)
    if owner is None:
        raise RuntimeError("execution admission 缺少显式 SessionThread owner")
    if (admission.session_id, admission.thread_id) != (
        owner.session_id,
        owner.thread_id,
    ):
        raise RuntimeError(
            "execution admission 与 SessionThread owner 冲突: "
            f"admission=({admission.session_id},{admission.thread_id}), "
            f"owner=({owner.session_id},{owner.thread_id})"
        )
    return admission


def set_current_execution_admission(
    admission: ExecutionAdmission,
    *,
    langgraph_thread_id: str,
) -> Token[ExecutionAdmission | None]:
    if not isinstance(admission, ExecutionAdmission):
        raise TypeError("execution admission 必须是 ExecutionAdmission")
    _validate_binding(admission, langgraph_thread_id=langgraph_thread_id)
    return _current_admission.set(admission)


def reset_current_execution_admission(
    token: Token[ExecutionAdmission | None],
) -> None:
    _current_admission.reset(token)


def current_execution_admission(
    *,
    langgraph_thread_id: str | None = None,
) -> ExecutionAdmission | None:
    admission = _current_admission.get()
    if admission is not None:
        _validate_binding(admission, langgraph_thread_id=langgraph_thread_id)
    return admission


def require_current_execution_admission(
    *,
    langgraph_thread_id: str | None = None,
) -> ExecutionAdmission:
    admission = current_execution_admission(
        langgraph_thread_id=langgraph_thread_id,
    )
    if admission is None:
        raise RuntimeError("LangGraph callback 缺少显式 execution admission")
    return admission


@contextmanager
def execution_admission_scope(
    admission: ExecutionAdmission,
    *,
    langgraph_thread_id: str,
) -> Iterator[ExecutionAdmission]:
    token = set_current_execution_admission(
        admission,
        langgraph_thread_id=langgraph_thread_id,
    )
    try:
        yield admission
    finally:
        reset_current_execution_admission(token)


__all__ = [
    "current_execution_admission",
    "execution_admission_scope",
    "require_current_execution_admission",
    "reset_current_execution_admission",
    "set_current_execution_admission",
]
