"""LangGraph callback 使用的显式 SessionThread owner context。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from app.domain.itemized.mutation_intents import MutationIntentOwner

_current_owner: ContextVar[MutationIntentOwner | None] = ContextVar(
    "current_session_thread_owner",
    default=None,
)


def set_current_session_thread_owner(
    owner: MutationIntentOwner,
    *,
    langgraph_thread_id: str,
) -> Token[MutationIntentOwner | None]:
    """绑定 owner，并验证它与 LangGraph configurable 中的 thread_id 一致。"""
    if not isinstance(owner, MutationIntentOwner):
        raise TypeError("SessionThread owner 必须是 MutationIntentOwner")
    if not isinstance(langgraph_thread_id, str) or not langgraph_thread_id:
        raise ValueError("LangGraph thread_id 必须是非空字符串")
    if owner.thread_id != langgraph_thread_id:
        raise ValueError(
            "SessionThread owner 与 LangGraph thread_id 冲突: "
            f"owner=({owner.session_id},{owner.thread_id}), "
            f"langgraph_thread_id={langgraph_thread_id}"
        )
    return _current_owner.set(owner)


def reset_current_session_thread_owner(
    token: Token[MutationIntentOwner | None],
) -> None:
    _current_owner.reset(token)


def current_session_thread_owner(
    *,
    langgraph_thread_id: str | None = None,
) -> MutationIntentOwner | None:
    owner = _current_owner.get()
    if (
        owner is not None
        and langgraph_thread_id is not None
        and owner.thread_id != langgraph_thread_id
    ):
        raise RuntimeError(
            "当前 SessionThread owner 与 LangGraph callback thread_id 冲突: "
            f"owner=({owner.session_id},{owner.thread_id}), "
            f"langgraph_thread_id={langgraph_thread_id}"
        )
    return owner


def require_current_session_thread_owner(
    *,
    langgraph_thread_id: str,
) -> MutationIntentOwner:
    owner = current_session_thread_owner(langgraph_thread_id=langgraph_thread_id)
    if owner is None:
        raise RuntimeError(
            "LangGraph callback 缺少显式 SessionThread owner: "
            f"thread_id={langgraph_thread_id}"
        )
    return owner


@contextmanager
def session_thread_owner_scope(
    owner: MutationIntentOwner,
    *,
    langgraph_thread_id: str,
) -> Iterator[MutationIntentOwner]:
    token = set_current_session_thread_owner(
        owner,
        langgraph_thread_id=langgraph_thread_id,
    )
    try:
        yield owner
    finally:
        reset_current_session_thread_owner(token)


__all__ = [
    "current_session_thread_owner",
    "require_current_session_thread_owner",
    "reset_current_session_thread_owner",
    "session_thread_owner_scope",
    "set_current_session_thread_owner",
]
