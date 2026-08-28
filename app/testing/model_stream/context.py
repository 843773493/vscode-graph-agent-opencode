from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from .errors import ModelStreamError

_REPLAY_SESSION_ID: ContextVar[str | None] = ContextVar(
    "model_stream_replay_session_id",
    default=None,
)


def current_replay_session_id() -> str | None:
    return _REPLAY_SESSION_ID.get()


@contextmanager
def replay_session(session_id: str) -> Iterator[None]:
    if not session_id.strip():
        raise ModelStreamError("replay_session_id 不能是空字符串")
    token: Token[str | None] = _REPLAY_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _REPLAY_SESSION_ID.reset(token)
