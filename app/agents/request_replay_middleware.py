from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any, TypedDict

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage


class PromptReplayComponent(TypedDict):
    source: str
    label: str
    operation: str
    content_blocks: list[dict[str, object]]


class PromptReplayTrace(TypedDict):
    observed_blocks: list[dict[str, object]]
    prompt_components: list[PromptReplayComponent]


_PROMPT_REPLAY_TRACE: ContextVar[PromptReplayTrace | None] = ContextVar(
    "llm_request_prompt_replay_trace",
    default=None,
)


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


def read_prompt_replay_components(
) -> list[PromptReplayComponent]:
    """读取当前模型调用的 Prompt 回放信息，不接触 Agent 上下文状态。"""
    raw_trace = _PROMPT_REPLAY_TRACE.get()
    if raw_trace is None:
        return []
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
            raise TypeError(f"Prompt replay component[{index}].content_blocks 必须是 list")
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
        raw_trace = _PROMPT_REPLAY_TRACE.get()
        if raw_trace is None:
            observed_blocks: list[dict[str, object]] = []
            components: list[PromptReplayComponent] = []
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

        if blocks == observed_blocks:
            return None

        is_append = (
            len(blocks) >= len(observed_blocks)
            and blocks[: len(observed_blocks)] == observed_blocks
        )
        operation = "append" if is_append else "replace"
        captured_blocks = (
            blocks[len(observed_blocks) :]
            if is_append
            else blocks
        )

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
