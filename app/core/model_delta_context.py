from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.core.model_request_context import current_summary_candidate


class ModelDeltaSink(Protocol):
    async def complete_tool_approval(self, message: ToolMessage, *, model_call_id: str) -> None: ...

    async def prepare_tool_execution(self, model_call_id: str) -> None: ...

    async def wait_tool_start(
        self, model_call_id: str, tool_call_id: str, callback_run_id: str,
    ) -> None: ...

    def reject_tool_start(self, callback_run_id: str, error: BaseException) -> None: ...

    async def fail_tool_invocation(
        self, model_call_id: str, tool_call_id: str, *, error: str, artifact: object = None,
    ) -> None: ...

    async def accept_message_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        model_call_id: str | None = None,
    ) -> None: ...


_CURRENT_MODEL_DELTA_SINK: ContextVar[ModelDeltaSink | None] = ContextVar(
    "boxteam_current_model_delta_sink",
    default=None,
)
_MODEL_RUN_ID_QUEUES: dict[int, deque[str]] = {}
_MODEL_RUN_ID_QUEUES_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class _ToolInvocationIdentity:
    model_call_id: str
    tool_call_id: str


_CURRENT_TOOL_INVOCATION: ContextVar[_ToolInvocationIdentity | None] = ContextVar(
    "boxteam_current_model_tool_invocation", default=None,
)


@contextmanager
def model_tool_invocation_scope(model_call_id: str, tool_call_id: str) -> Iterator[None]:
    """把 ToolNode 已验证的原调用身份传给同一调用的 LangChain callback。"""
    token = _CURRENT_TOOL_INVOCATION.set(_ToolInvocationIdentity(model_call_id, tool_call_id))
    try:
        yield
    finally:
        _CURRENT_TOOL_INVOCATION.reset(token)


def set_current_model_delta_sink(sink: ModelDeltaSink) -> Token[ModelDeltaSink | None]:
    return _CURRENT_MODEL_DELTA_SINK.set(sink)


def reset_current_model_delta_sink(token: Token[ModelDeltaSink | None]) -> None:
    _CURRENT_MODEL_DELTA_SINK.reset(token)


def get_current_model_delta_sink() -> ModelDeltaSink | None:
    return _CURRENT_MODEL_DELTA_SINK.get()


def register_model_run_id(sink: ModelDeltaSink, run_id: object) -> None:
    if run_id is None:
        raise RuntimeError("LangChain model start 回调缺少 run_id")
    with _MODEL_RUN_ID_QUEUES_LOCK:
        _MODEL_RUN_ID_QUEUES.setdefault(id(sink), deque()).append(str(run_id))


def claim_model_run_id(sink: ModelDeltaSink) -> str | None:
    with _MODEL_RUN_ID_QUEUES_LOCK:
        queue = _MODEL_RUN_ID_QUEUES.get(id(sink))
        if not queue:
            return None
        run_id = queue.popleft()
        if not queue:
            _MODEL_RUN_ID_QUEUES.pop(id(sink), None)
        return run_id


def acknowledge_model_run_id(sink: ModelDeltaSink, run_id: object) -> None:
    if run_id is None:
        return
    run_id_text = str(run_id)
    with _MODEL_RUN_ID_QUEUES_LOCK:
        queue = _MODEL_RUN_ID_QUEUES.get(id(sink))
        if not queue:
            return
        if queue[0] == run_id_text:
            queue.popleft()
        else:
            try:
                queue.remove(run_id_text)
            except ValueError:
                return
        if not queue:
            _MODEL_RUN_ID_QUEUES.pop(id(sink), None)


def clear_model_run_id_queue(sink: ModelDeltaSink) -> None:
    with _MODEL_RUN_ID_QUEUES_LOCK:
        _MODEL_RUN_ID_QUEUES.pop(id(sink), None)


def resolve_model_run_id(
    sink: ModelDeltaSink | None,
    run_manager: object | None,
) -> str | None:
    """解析 provider delta 对应的 LangChain model run 身份。

    正常路径直接使用 provider 收到的 run manager；部分 Agent 图在调用
    provider 时没有把它继续传入，内部身份 callback 会提前把同一 run ID
    登记到 sink 的短期队列中，作为唯一的生命周期兜底。身份解析集中在
    这里，避免不同 provider 各自实现不同的配对规则。
    """
    raw_run_id = getattr(run_manager, "run_id", None)
    if sink is not None:
        if raw_run_id is None:
            raw_run_id = claim_model_run_id(sink)
        else:
            acknowledge_model_run_id(sink, raw_run_id)
    return str(raw_run_id) if raw_run_id is not None else None


class AgentRunLifecycleCallbackHandler(BaseCallbackHandler):
    """转交真实 LangChain run 身份，并等待 sink 确认工具可以开始。"""

    # 必须与 astream_events callback 并行；inline 等待会挡住开始事件入队。
    run_inline = False
    raise_error = True

    def __init__(self, sink: ModelDeltaSink | None = None) -> None:
        super().__init__()
        self._sink = sink

    def on_chat_model_start(self, *_args: object, **kwargs: object) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            raise RuntimeError("LangChain model start 回调缺少 run_id")
        if self._sink is not None:
            candidate = current_summary_candidate()
            if candidate is not None:
                register_candidate = getattr(self._sink, "register_summary_candidate", None)
                if not callable(register_candidate):
                    raise TypeError("摘要调用需要支持隐藏候选正文的 ModelDeltaSink")
                register_candidate(str(run_id))
            register_model_run_id(self._sink, run_id)

    async def on_tool_start(self, *_args: object, **kwargs: object) -> None:
        if self._sink is None:
            return
        # 扩展封装内部的 BaseTool 调用没有 provider call，不登记第二次 start。
        if kwargs.get("tool_call_id") is None:
            return
        run_id = kwargs.get("run_id")
        if run_id is None:
            raise RuntimeError("LangChain tool start 回调缺少 run_id")
        try:
            invocation = _CURRENT_TOOL_INVOCATION.get()
            if invocation is None:
                raise RuntimeError("LangChain tool start 回调缺少 emitting tool invocation")
            if kwargs.get("tool_call_id") != invocation.tool_call_id:
                raise RuntimeError("LangChain tool start 回调与 ToolNode 原调用 ID 不一致")
            await self._sink.wait_tool_start(
                invocation.model_call_id, invocation.tool_call_id, callback_run_id=str(run_id),
            )
        except BaseException as error:
            self._sink.reject_tool_start(callback_run_id=str(run_id), error=error)
            raise
