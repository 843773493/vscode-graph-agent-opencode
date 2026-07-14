from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)


MAX_MODEL_CALLS_PER_ATTEMPT = 3
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

_REASONING_BLOCK_TYPES = frozenset({"reasoning", "thinking"})


@dataclass(frozen=True, slots=True)
class ModelCallExchange:
    request: dict[str, object]
    response: dict[str, object] | None = None
    error: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ToolCallProbeResult:
    tool_call: dict[str, object] | None
    model_calls: int
    reasoning_only_calls: int
    transient_retries: int = 0
    failure: str | None = None
    exchanges: tuple[ModelCallExchange, ...] = ()


class ToolCallProbeInvocationError(RuntimeError):
    def __init__(
        self,
        *,
        cause: Exception,
        model_calls: int,
        reasoning_only_calls: int,
        transient_retries: int = 0,
        exchanges: tuple[ModelCallExchange, ...] = (),
    ) -> None:
        super().__init__(f"模型调用失败: {type(cause).__name__}: {cause}")
        self.cause = cause
        self.model_calls = model_calls
        self.reasoning_only_calls = reasoning_only_calls
        self.transient_retries = transient_retries
        self.exchanges = exchanges


async def probe_tool_call_with_transient_retries(
    *,
    model: Any,
    tool: BaseTool,
    messages: list[BaseMessage],
    max_model_calls: int = MAX_MODEL_CALLS_PER_ATTEMPT,
    retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS_SECONDS,
) -> ToolCallProbeResult:
    if len(retry_delays) != MAX_TRANSIENT_RETRIES:
        raise ValueError(f"网络重试延迟必须配置 {MAX_TRANSIENT_RETRIES} 个")

    total_model_calls = 0
    total_reasoning_only_calls = 0
    all_exchanges: list[ModelCallExchange] = []
    for retry_index in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            result = await probe_tool_call(
                model=model,
                tool=tool,
                messages=messages,
                max_model_calls=max_model_calls,
            )
            return ToolCallProbeResult(
                tool_call=result.tool_call,
                model_calls=total_model_calls + result.model_calls,
                reasoning_only_calls=(
                    total_reasoning_only_calls + result.reasoning_only_calls
                ),
                transient_retries=retry_index,
                failure=result.failure,
                exchanges=tuple([*all_exchanges, *result.exchanges]),
            )
        except ToolCallProbeInvocationError as error:
            total_model_calls += error.model_calls
            total_reasoning_only_calls += error.reasoning_only_calls
            all_exchanges.extend(error.exchanges)
            should_retry = is_transient_provider_error(error.cause)
            if not should_retry or retry_index == MAX_TRANSIENT_RETRIES:
                raise ToolCallProbeInvocationError(
                    cause=error.cause,
                    model_calls=total_model_calls,
                    reasoning_only_calls=total_reasoning_only_calls,
                    transient_retries=retry_index,
                    exchanges=tuple(all_exchanges),
                ) from error
            await asyncio.sleep(retry_delays[retry_index])

    raise AssertionError("网络重试循环意外结束")


def is_transient_provider_error(error: Exception) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                ConnectionError,
                TimeoutError,
                httpx.TransportError,
                httpx.TimeoutException,
                APIConnectionError,
                BadGatewayError,
                InternalServerError,
                RateLimitError,
                ServiceUnavailableError,
                Timeout,
            ),
        ):
            return True
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and (
            status_code in {408, 429} or 500 <= status_code <= 599
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


async def probe_tool_call(
    *,
    model: Any,
    tool: BaseTool,
    messages: list[BaseMessage],
    max_model_calls: int = MAX_MODEL_CALLS_PER_ATTEMPT,
) -> ToolCallProbeResult:
    """允许纯 reasoning 继续，但不重试普通正文或错误工具调用。"""
    if max_model_calls < 1:
        raise ValueError("max_model_calls 必须大于 0")

    bound_model = model.bind_tools([tool])
    conversation = list(messages)
    reasoning_only_calls = 0
    exchanges: list[ModelCallExchange] = []

    for model_call in range(1, max_model_calls + 1):
        request_payload = {
            "model_call": model_call,
            "messages": [message.model_dump(mode="json") for message in conversation],
        }
        try:
            response = await bound_model.ainvoke(conversation)
        except Exception as error:
            exchanges.append(
                ModelCallExchange(
                    request=request_payload,
                    error={"type": type(error).__name__, "message": str(error)},
                )
            )
            raise ToolCallProbeInvocationError(
                cause=error,
                model_calls=model_call,
                reasoning_only_calls=reasoning_only_calls,
                exchanges=tuple(exchanges),
            ) from error
        if not isinstance(response, AIMessage):
            error = TypeError(f"模型响应不是 AIMessage: {type(response).__name__}")
            exchanges.append(
                ModelCallExchange(
                    request=request_payload,
                    error={"type": type(error).__name__, "message": str(error)},
                )
            )
            raise ToolCallProbeInvocationError(
                cause=error,
                model_calls=model_call,
                reasoning_only_calls=reasoning_only_calls,
                exchanges=tuple(exchanges),
            ) from error

        exchanges.append(
            ModelCallExchange(
                request=request_payload,
                response=response.model_dump(mode="json"),
            )
        )

        matching_call = next(
            (call for call in response.tool_calls if call.get("name") == tool.name),
            None,
        )
        if matching_call is not None:
            return ToolCallProbeResult(
                tool_call=dict(matching_call),
                model_calls=model_call,
                reasoning_only_calls=reasoning_only_calls,
                exchanges=tuple(exchanges),
            )

        if response.tool_calls:
            called_names = [str(call.get("name")) for call in response.tool_calls]
            return ToolCallProbeResult(
                tool_call=None,
                model_calls=model_call,
                reasoning_only_calls=reasoning_only_calls,
                failure=f"模型调用了错误工具: {called_names!r}; 预期={tool.name}",
                exchanges=tuple(exchanges),
            )

        if not _is_reasoning_only(response):
            return ToolCallProbeResult(
                tool_call=None,
                model_calls=model_call,
                reasoning_only_calls=reasoning_only_calls,
                failure=f"模型没有调用 {tool.name}，且响应不是纯 reasoning",
                exchanges=tuple(exchanges),
            )

        reasoning_only_calls += 1
        if model_call == max_model_calls:
            break
        conversation.extend(
            [
                response,
                HumanMessage(
                    content=(
                        "<system_reminder>你刚才只完成了 reasoning，测试任务尚未完成。"
                        f"请继续执行，并通过真实 tool call 调用 {tool.name}；"
                        "不要在普通正文中描述或模拟调用。</system_reminder>"
                    )
                ),
            ]
        )

    return ToolCallProbeResult(
        tool_call=None,
        model_calls=max_model_calls,
        reasoning_only_calls=reasoning_only_calls,
        failure=(
            f"模型连续 {reasoning_only_calls} 次只返回 reasoning，"
            f"仍未调用 {tool.name}"
        ),
        exchanges=tuple(exchanges),
    )


def _is_reasoning_only(response: AIMessage) -> bool:
    content = response.content
    if isinstance(content, str):
        reasoning_content = response.additional_kwargs.get("reasoning_content")
        return not content.strip() and bool(reasoning_content)
    if not isinstance(content, list) or not content:
        return False

    saw_reasoning = False
    for block in content:
        if isinstance(block, str):
            if block.strip():
                return False
            continue
        if not isinstance(block, dict):
            return False
        block_type = block.get("type")
        if block_type in _REASONING_BLOCK_TYPES:
            saw_reasoning = True
            continue
        if block_type == "text" and not str(block.get("text") or "").strip():
            continue
        return False
    return saw_reasoning
