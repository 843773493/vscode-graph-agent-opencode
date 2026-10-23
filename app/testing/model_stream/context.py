"""回放消费真实封存 dispatch 的精确 SessionThread/model-call 身份。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from app.core.identifier import validate_session_id, validate_thread_id

from .errors import ModelStreamMatchError


@dataclass(frozen=True, slots=True)
class ModelCallIdentity:
    session_id: str
    thread_id: str
    model_call_id: str

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        validate_thread_id(self.thread_id)
        if UUID(self.model_call_id).version != 7:
            raise ModelStreamMatchError("replay model_call_id 必须是显式 UUIDv7")

    def to_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "thread_id": self.thread_id,
                "model_call_id": self.model_call_id}


_REPLAY_CALL: ContextVar[ModelCallIdentity | None] = ContextVar("model_stream_replay_call", default=None)


def current_model_call_identity() -> ModelCallIdentity:
    explicit = _REPLAY_CALL.get()
    if explicit is not None:
        return explicit
    # 此处只读取执行 owner 已有的不可变绑定；不扫描存储或推断 main thread。
    from app.agents.sealed_assembly_dispatch import read_sealed_assembly_dispatch
    from app.core.session_thread_context import require_current_session_thread_owner

    try:
        dispatch = read_sealed_assembly_dispatch(invocation_purpose="agent")
    except ValueError as error:
        raise ModelStreamMatchError("call_identity 缺少显式 SessionThread/model-call dispatch") from error
    if dispatch is None:
        raise ModelStreamMatchError("call_identity 缺少 sealed dispatch")
    snapshot = dispatch.snapshot
    owner = require_current_session_thread_owner(langgraph_thread_id=snapshot.thread_id)
    if (owner.session_id, owner.thread_id) != (snapshot.session_id, snapshot.thread_id):
        raise ModelStreamMatchError("回放 dispatch 与实际 SessionThread owner 冲突")
    return ModelCallIdentity(owner.session_id, owner.thread_id, dispatch.callback_model_call_id)


@contextmanager
def replay_model_call(identity: ModelCallIdentity) -> Iterator[None]:
    """仅供直接 transport 测试显式注入；真实 Agent 沿原 sealed dispatch。"""
    if not isinstance(identity, ModelCallIdentity):
        raise TypeError("replay_model_call 需要 ModelCallIdentity")
    token = _REPLAY_CALL.set(identity)
    try:
        yield
    finally:
        _REPLAY_CALL.reset(token)
