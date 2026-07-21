from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import date, datetime
from enum import Enum
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from pydantic import SecretStr


_REDACTED_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "api-key",
    "cookie",
    "proxy_authorization",
    "set-cookie",
    "x-api-key",
    "x-goog-api-key",
}
_UPSTREAM_ATTEMPTS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "llm_upstream_attempts",
    default=None,
)


def _safe_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _REDACTED_KEYS:
        return "[REDACTED]"
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _safe_value(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _safe_value(value.model_dump(exclude_none=True, mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            item_key = str(raw_key)
            if item_key.casefold() == "headers" and isinstance(raw_value, Mapping):
                result[item_key] = {
                    str(header): _safe_value(header_value, key=str(header))
                    for header, header_value in raw_value.items()
                }
            else:
                result[item_key] = _safe_value(raw_value, key=item_key)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(item) for item in value]
    return str(value)


def begin_upstream_capture() -> Token[list[dict[str, Any]] | None]:
    return _UPSTREAM_ATTEMPTS.set([])


def end_upstream_capture(
    token: Token[list[dict[str, Any]] | None],
) -> list[dict[str, Any]]:
    attempts = _UPSTREAM_ATTEMPTS.get()
    if attempts is None:
        raise RuntimeError("upstream 请求采集上下文未初始化")
    result = [_safe_value(attempt) for attempt in attempts]
    _UPSTREAM_ATTEMPTS.reset(token)
    return result


def _request_payload(
    model: str,
    messages: Any,
    kwargs: Mapping[str, Any],
    fallback_request: Mapping[str, Any] | None,
) -> dict[str, Any]:
    additional_args = kwargs.get("additional_args")
    if isinstance(additional_args, Mapping):
        complete_input = additional_args.get("complete_input_dict")
        if isinstance(complete_input, Mapping) and complete_input:
            return _safe_value(complete_input)
    if fallback_request is not None:
        return _safe_value(fallback_request)

    payload: dict[str, Any] = {"model": model}
    optional_params = kwargs.get("optional_params")
    if isinstance(optional_params, Mapping):
        payload.update(optional_params)
    raw_input = kwargs.get("input")
    if raw_input is not None:
        payload["input"] = raw_input
    elif messages is not None:
        payload["messages"] = messages
    return _safe_value(payload)


class UpstreamRequestTraceCallback(CustomLogger):
    """把 LiteLLM 单次真实调用附加到当前 LangChain 模型请求日志。"""

    def __init__(self, *, fallback_request: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._fallback_request = (
            dict(fallback_request) if fallback_request is not None else None
        )

    def log_pre_api_call(self, model: str, messages: Any, kwargs: dict[str, Any]) -> None:
        attempts = _UPSTREAM_ATTEMPTS.get()
        if attempts is None:
            return
        additional_args = kwargs.get("additional_args")
        api_base = (
            additional_args.get("api_base")
            if isinstance(additional_args, Mapping)
            else None
        )
        attempts.append(
            {
                "litellm_call_id": kwargs.get("litellm_call_id"),
                "call_type": kwargs.get("call_type"),
                "provider": kwargs.get("custom_llm_provider"),
                "model": model,
                "api_base": str(api_base) if api_base is not None else None,
                "request": _request_payload(
                    model,
                    messages,
                    kwargs,
                    self._fallback_request,
                ),
                "response": None,
                "error": None,
            }
        )

    def _finish(self, kwargs: Mapping[str, Any], *, response: Any, error: Any) -> None:
        attempts = _UPSTREAM_ATTEMPTS.get()
        if attempts is None:
            return
        call_id = kwargs.get("litellm_call_id")
        for attempt in reversed(attempts):
            if attempt.get("litellm_call_id") == call_id:
                attempt["response"] = _safe_value(response)
                attempt["error"] = _safe_value(error)
                return
        raise RuntimeError(f"LiteLLM 回调找不到对应的 upstream attempt: {call_id!r}")

    def log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        self._finish(kwargs, response=response_obj, error=None)

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        self._finish(kwargs, response=response_obj, error=None)

    def log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        self._finish(kwargs, response=None, error=response_obj)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        self._finish(kwargs, response=None, error=response_obj)


def attach_upstream_trace_callback(
    params: Mapping[str, Any],
    *,
    fallback_request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(params)
    existing = result.get("callbacks")
    callbacks = list(existing) if isinstance(existing, Sequence) else []
    callbacks.append(
        UpstreamRequestTraceCallback(fallback_request=fallback_request)
    )
    result["callbacks"] = callbacks
    return result
