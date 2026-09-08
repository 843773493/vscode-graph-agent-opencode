from __future__ import annotations

import copy
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from threading import Lock
from typing import Any, TypedDict

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool


class PromptReplayComponent(TypedDict):
    source: str
    label: str
    operation: str
    content_blocks: list[dict[str, object]]


class PromptReplayTrace(TypedDict):
    observed_blocks: list[dict[str, object]]
    prompt_components: list[PromptReplayComponent]
    tool_snapshot: list[dict[str, object]]


class RequestReplaySnapshot(TypedDict):
    system_blocks: list[dict[str, object]]
    tool_snapshot: list[dict[str, object]]
    prompt_components: list[PromptReplayComponent]


_PROMPT_REPLAY_TRACE: ContextVar[PromptReplayTrace | None] = ContextVar(
    "llm_request_prompt_replay_trace",
    default=None,
)
_ACTIVE_REQUEST_SNAPSHOT: ContextVar[dict[str, object] | None] = ContextVar(
    "llm_active_request_snapshot",
    default=None,
)
_REQUEST_REPLAY_SNAPSHOTS: dict[tuple[str, str], deque[RequestReplaySnapshot]] = {}
_REQUEST_REPLAY_SNAPSHOTS_LOCK = Lock()
_NATIVE_REQUEST_PROJECTION: ContextVar[dict[str, object] | None] = ContextVar(
    "sealed_native_request_projection", default=None
)


@contextmanager
def native_request_projection_scope(
    projection: Mapping[str, object] | None,
) -> Iterator[None]:
    """只在当前 provider handler 生命周期内传递 Saver 验证过的原生请求。

    正文不放进 LangChain model_settings/callback metadata；ContextVar 隔离
    并发请求，并随 Responses payload 的 asyncio.to_thread 传递。
    """
    if projection is not None and (
        not isinstance(projection.get("session_id"), str)
        or not isinstance(projection.get("assembly_id"), str)
        or not isinstance(projection.get("plan_hash"), str)
        or not isinstance(projection.get("selection"), list)
        or not isinstance(projection.get("request"), dict)
    ):
        raise TypeError("source-mismatch: native projection 缺少 sealed manifest")
    token = _NATIVE_REQUEST_PROJECTION.set(
        copy.deepcopy(dict(projection)) if projection is not None else None
    )
    try:
        yield
    finally:
        _NATIVE_REQUEST_PROJECTION.reset(token)


def read_native_request_projection() -> dict[str, object] | None:
    """返回只属于本次模型调用的独立副本，禁止 provider 原地改写 manifest。"""
    return copy.deepcopy(_NATIVE_REQUEST_PROJECTION.get())


def _system_content_blocks(message: SystemMessage | None) -> list[dict[str, object]]:
    if message is None:
        return []
    blocks: list[dict[str, object]] = []
    for index, block in enumerate(message.content_blocks):
        if not isinstance(block, Mapping):
            raise TypeError(
                "SystemMessage.content_blocks 中出现非 mapping 元素: "
                f"index={index}, type={type(block).__name__}"
            )
        blocks.append({str(key): value for key, value in block.items()})
    return blocks


def read_prompt_replay_components() -> list[PromptReplayComponent]:
    """读取当前模型调用的 Prompt 回放信息，不接触 Agent 上下文状态。"""
    raw_trace = _PROMPT_REPLAY_TRACE.get()
    if raw_trace is None:
        active = _ACTIVE_REQUEST_SNAPSHOT.get()
        if active is None:
            return []
        blocks = active.get("system_blocks", [])
        if not isinstance(blocks, list):
            raise TypeError("active request snapshot.system_blocks 必须是 list")
        return [
            {
                "source": "provider_request",
                "label": "assembled system prompt",
                "operation": "replace",
                "content_blocks": [
                    dict(block) for block in blocks if isinstance(block, Mapping)
                ],
            }
        ]
    if not isinstance(raw_trace, Mapping):
        raise TypeError("Prompt replay trace 必须是 mapping")
    raw_components = raw_trace.get("prompt_components")
    if not isinstance(raw_components, list):
        raise TypeError("Prompt replay trace.prompt_components 必须是 list")

    components: list[PromptReplayComponent] = []
    for index, raw_component in enumerate(raw_components):
        if not isinstance(raw_component, Mapping):
            raise TypeError(
                f"Prompt replay trace.prompt_components[{index}] 必须是 mapping"
            )
        source = raw_component.get("source")
        label = raw_component.get("label")
        operation = raw_component.get("operation")
        content_blocks = raw_component.get("content_blocks")
        if not isinstance(source, str) or not source:
            raise TypeError(f"Prompt replay component[{index}].source 必须是非空字符串")
        if not isinstance(label, str) or not label:
            raise TypeError(f"Prompt replay component[{index}].label 必须是非空字符串")
        if operation not in {"append", "replace"}:
            raise TypeError(
                f"Prompt replay component[{index}].operation 必须是 append/replace"
            )
        if not isinstance(content_blocks, list):
            raise TypeError(
                f"Prompt replay component[{index}].content_blocks 必须是 list"
            )
        normalized_blocks: list[dict[str, object]] = []
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, Mapping):
                raise TypeError(
                    f"Prompt replay component[{index}].content_blocks[{block_index}] "
                    "必须是 mapping"
                )
            normalized_blocks.append({str(key): value for key, value in block.items()})
        components.append(
            {
                "source": source,
                "label": label,
                "operation": operation,
                "content_blocks": normalized_blocks,
            }
        )
    return components


def _tool_snapshot(request: ModelRequest[Any]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, tool in enumerate(request.tools or ()):
        if isinstance(tool, BaseTool):
            value: object = {
                "type": "tool",
                "name": tool.name,
                "description": tool.description,
                "args": tool.args,
            }
        elif isinstance(tool, Mapping):
            value = dict(tool)
        else:
            raise TypeError(
                f"Prompt replay tool[{index}] 不支持的类型: {type(tool).__name__}"
            )
        if not isinstance(value, Mapping):
            raise TypeError(f"Prompt replay tool[{index}] 必须是 mapping")
        result.append({str(key): child for key, child in value.items()})
    return result


def read_prompt_replay_tool_snapshot() -> list[dict[str, object]]:
    """读取本次模型请求实际可见的 tool schema 快照。"""
    raw_trace = _PROMPT_REPLAY_TRACE.get()
    if raw_trace is None:
        active = _ACTIVE_REQUEST_SNAPSHOT.get()
        if active is None:
            return []
        raw_tools = active.get("tool_snapshot", [])
        if not isinstance(raw_tools, list):
            raise TypeError("active request snapshot.tool_snapshot 必须是 list")
        return [dict(tool) for tool in raw_tools if isinstance(tool, Mapping)]
    if not isinstance(raw_trace, Mapping):
        raise TypeError("Prompt replay trace 必须是 mapping")
    raw_tools = raw_trace.get("tool_snapshot", [])
    if not isinstance(raw_tools, list):
        raise TypeError("Prompt replay trace.tool_snapshot 必须是 list")
    result: list[dict[str, object]] = []
    for index, raw_tool in enumerate(raw_tools):
        if not isinstance(raw_tool, Mapping):
            raise TypeError(
                f"Prompt replay trace.tool_snapshot[{index}] 必须是 mapping"
            )
        result.append({str(key): child for key, child in raw_tool.items()})
    return result


def set_active_request_snapshot(
    request: ModelRequest[Any],
) -> Token[dict[str, object] | None]:
    """在实际 provider handler 生命周期内暴露最终 request 的只读快照。"""
    return _ACTIVE_REQUEST_SNAPSHOT.set(
        {
            "system_blocks": _system_content_blocks(request.system_message),
            "tool_snapshot": _tool_snapshot(request),
        }
    )


def reset_active_request_snapshot(
    token: Token[dict[str, object] | None],
) -> None:
    _ACTIVE_REQUEST_SNAPSHOT.reset(token)


def publish_request_replay_snapshot(
    session_id: str,
    turn_id: str,
    request: ModelRequest[Any],
) -> RequestReplaySnapshot:
    """把最终模型请求复制到跨事件流 task 可读取的短生命周期登记表。"""
    if not session_id or not turn_id:
        raise ValueError("request replay snapshot 缺少 session_id/turn_id")
    snapshot: RequestReplaySnapshot = {
        "system_blocks": _system_content_blocks(request.system_message),
        "tool_snapshot": _tool_snapshot(request),
        "prompt_components": read_prompt_replay_components(),
    }
    with _REQUEST_REPLAY_SNAPSHOTS_LOCK:
        _REQUEST_REPLAY_SNAPSHOTS.setdefault((session_id, turn_id), deque()).append(
            snapshot
        )
    return snapshot


def consume_request_replay_snapshot(
    session_id: str,
    turn_id: str,
) -> RequestReplaySnapshot | None:
    """按模型事件顺序消费同一 turn 的最终 request snapshot。"""
    with _REQUEST_REPLAY_SNAPSHOTS_LOCK:
        snapshots = _REQUEST_REPLAY_SNAPSHOTS.get((session_id, turn_id))
        if not snapshots:
            return None
        snapshot = snapshots.popleft()
        if not snapshots:
            del _REQUEST_REPLAY_SNAPSHOTS[(session_id, turn_id)]
        return snapshot


def discard_request_replay_snapshot(
    session_id: str,
    turn_id: str,
    snapshot: RequestReplaySnapshot,
) -> None:
    """模型事件丢失时清除尚未消费的 request snapshot，避免串到后续重试。"""
    with _REQUEST_REPLAY_SNAPSHOTS_LOCK:
        snapshots = _REQUEST_REPLAY_SNAPSHOTS.get((session_id, turn_id))
        if not snapshots:
            return
        try:
            snapshots.remove(snapshot)
        except ValueError:
            return
        if not snapshots:
            del _REQUEST_REPLAY_SNAPSHOTS[(session_id, turn_id)]


class PromptReplayCaptureMiddleware(AgentMiddleware[Any, Any, Any]):
    """记录每个 middleware 新增的 system prompt 块，仅在本次模型请求内传递。"""

    def __init__(
        self,
        *,
        source: str,
        label: str,
        capture_id: str | None = None,
    ) -> None:
        self._source = source
        self._label = label
        self._capture_id = capture_id or source

    @property
    def name(self) -> str:
        """LangChain 以 middleware name 判重，每个采集位置必须拥有独立身份。"""
        return f"PromptReplayCaptureMiddleware[{self._capture_id}]"

    def _capture(self, request: ModelRequest[Any]) -> PromptReplayTrace | None:
        blocks = _system_content_blocks(request.system_message)
        tools = _tool_snapshot(request)
        raw_trace = _PROMPT_REPLAY_TRACE.get()
        if raw_trace is None:
            observed_blocks: list[dict[str, object]] = []
            components: list[PromptReplayComponent] = []
            observed_tools: list[dict[str, object]] = []
        else:
            if not isinstance(raw_trace, Mapping):
                raise TypeError("Prompt replay trace 必须是 mapping")
            raw_observed_blocks = raw_trace.get("observed_blocks")
            if not isinstance(raw_observed_blocks, list):
                raise TypeError("Prompt replay trace.observed_blocks 必须是 list")
            observed_blocks = []
            for index, block in enumerate(raw_observed_blocks):
                if not isinstance(block, Mapping):
                    raise TypeError(
                        f"Prompt replay trace.observed_blocks[{index}] 必须是 mapping"
                    )
                observed_blocks.append(
                    {str(key): value for key, value in block.items()}
                )
            components = read_prompt_replay_components()
            raw_observed_tools = raw_trace.get("tool_snapshot", [])
            if not isinstance(raw_observed_tools, list):
                raise TypeError("Prompt replay trace.tool_snapshot 必须是 list")
            observed_tools = []
            for index, tool in enumerate(raw_observed_tools):
                if not isinstance(tool, Mapping):
                    raise TypeError(
                        f"Prompt replay trace.tool_snapshot[{index}] 必须是 mapping"
                    )
                observed_tools.append({str(key): value for key, value in tool.items()})

        if blocks == observed_blocks and tools == observed_tools:
            return None

        is_append = (
            len(blocks) >= len(observed_blocks)
            and blocks[: len(observed_blocks)] == observed_blocks
        )
        operation = "append" if is_append else "replace"
        captured_blocks = blocks[len(observed_blocks) :] if is_append else blocks

        return {
            "observed_blocks": blocks,
            "prompt_components": [
                *components,
                {
                    "source": self._source,
                    "label": self._label,
                    "operation": operation,
                    "content_blocks": captured_blocks,
                },
            ],
            "tool_snapshot": tools,
        }

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        trace = self._capture(request)
        if trace is None:
            return handler(request)
        token = _PROMPT_REPLAY_TRACE.set(trace)
        try:
            return handler(request)
        finally:
            _PROMPT_REPLAY_TRACE.reset(token)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        trace = self._capture(request)
        if trace is None:
            return await handler(request)
        token = _PROMPT_REPLAY_TRACE.set(trace)
        try:
            return await handler(request)
        finally:
            _PROMPT_REPLAY_TRACE.reset(token)
