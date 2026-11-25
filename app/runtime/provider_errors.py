"""实际模型错误分类时才加载 Provider SDK 的异常类型。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from app.core import failures


def is_context_overflow(error: Exception) -> bool:
    """仅识别原 Provider 边界确认的 SDK 容量错误，不匹配任意错误正文。"""
    if not failures.is_provider_failure(error):
        return False
    from openai import BadRequestError

    from app.runtime.litellm_runtime import litellm

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, litellm.ContextWindowExceededError):
            return True
        if isinstance(current, (litellm.BadRequestError, BadRequestError)):
            body = current.body
            if isinstance(body, Mapping):
                detail = body.get("error", body)
                if isinstance(detail, Mapping) and detail.get("code") == "context_length_exceeded":
                    return True
        current = current.__cause__
    return False


def is_transient_provider_error(error: Exception) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    sdk_error_types: tuple[type[BaseException], ...] | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if failures.is_provider_failure(current):
            if isinstance(
                current,
                (
                    ConnectionError,
                    TimeoutError,
                    httpx.TransportError,
                    httpx.TimeoutException,
                ),
            ):
                return True
            status_code = getattr(current, "status_code", None)
            if isinstance(status_code, int) and (
                status_code in {408, 429} or 500 <= status_code <= 599
            ):
                return True
            if sdk_error_types is None:
                from app.runtime.litellm_runtime import litellm

                sdk_error_types = (
                    litellm.APIConnectionError,
                    litellm.BadGatewayError,
                    litellm.InternalServerError,
                    litellm.RateLimitError,
                    litellm.ServiceUnavailableError,
                    litellm.Timeout,
                )
            if isinstance(current, sdk_error_types):
                return True
        current = current.__cause__
    return False
