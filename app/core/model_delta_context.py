from __future__ import annotations

from collections import deque
from contextvars import ContextVar, Token
from threading import Lock
from typing import Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk


class ModelDeltaSink(Protocol):
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


class ModelRunIdentityCallbackHandler(BaseCallbackHandler):
    """在 provider 执行前暴露同一次 LangChain model run 的稳定身份。"""

    def __init__(self, sink: ModelDeltaSink | None = None) -> None:
        super().__init__()
        self._sink = sink

    def on_chat_model_start(self, *_args: object, **kwargs: object) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            raise RuntimeError("LangChain model start 回调缺少 run_id")
        if self._sink is not None:
            register_model_run_id(self._sink, run_id)
