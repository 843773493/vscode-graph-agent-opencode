from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import date, datetime
from enum import Enum
from typing import Any

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


def record_upstream_response(response: Any) -> None:
    """记录流式 Responses 在 terminal event 中携带的完整响应。"""
    if response is None:
        return
    _fill_pending(key="response", value=response)


def record_upstream_error(error: Any) -> None:
    """记录当前模型调用中尚未成功收尾的上游 attempt 的失败原因。"""
    if error is None:
        return
    _fill_pending(key="error", value=error)


def record_upstream_request(
    *,
    request: Any,
    model: str | None,
    provider: str | None,
    api_base: str | None,
    call_type: str,
) -> None:
    """在 Provider 发起上游请求前登记本次 attempt。

    先按 Provider 已知信息登记，保证建立流之前的失败也有据可查；调用建立后由
    ``apply_upstream_call_details`` 用 LiteLLM 解析结果校正。
    """
    attempts = _UPSTREAM_ATTEMPTS.get()
    if attempts is None:
        return
    attempts.append(
        {
            "call_type": call_type,
            "provider": provider,
            "model": model,
            "api_base": api_base,
            "request": _safe_value(request),
            "response": None,
            "error": None,
        }
    )


def apply_upstream_call_details(stream: Any) -> None:
    """用 LiteLLM 流对象上已解析的调用细节校正最后一条 attempt。

    取值与旧的 pre-call 回调同源，因此 api_base / call_type / request 与之前一致。
    TODO: LiteLLM 暴露公开的调用细节查询接口后，替换对 logging_obj 私有字段的读取。
    """
    attempts = _UPSTREAM_ATTEMPTS.get()
    if not attempts:
        return
    logging_obj = getattr(stream, "logging_obj", None)
    model_call_details = getattr(logging_obj, "model_call_details", None)
    if not isinstance(model_call_details, Mapping):
        raise TypeError(
            "LiteLLM 流对象缺少 model_call_details，无法校正 upstream attempt"
        )
    attempt = attempts[-1]
    additional_args = model_call_details.get("additional_args")
    if isinstance(additional_args, Mapping):
        api_base = additional_args.get("api_base")
        if api_base is not None:
            attempt["api_base"] = str(api_base)
        complete_input = additional_args.get("complete_input_dict")
        if isinstance(complete_input, Mapping) and complete_input:
            attempt["request"] = _safe_value(complete_input)
    for key, source in (
        ("call_type", "call_type"),
        ("provider", "custom_llm_provider"),
        ("model", "model"),
    ):
        value = model_call_details.get(source)
        if value is not None:
            attempt[key] = _safe_value(value)


def _fill_pending(*, key: str, value: Any) -> None:
    """把终结结果写入最后一条尚未收尾的 attempt。"""
    attempts = _UPSTREAM_ATTEMPTS.get()
    if attempts is None:
        return
    for attempt in reversed(attempts):
        if attempt.get("response") is None and attempt.get("error") is None:
            attempt[key] = _safe_value(value)
            return

